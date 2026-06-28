import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth.claims import TokenClaims
from app.auth.jwt_validator import get_current_claims
from app.auth.tool_permissions import disallowed_tools
from app.config.settings import settings
from app.llm.adapters.registry import get_adapter, is_provider_enabled
from app.llm.service import (
    NormalizedLLMRequest,
    StreamEvent,
    normalize_provider_name,
    reasoning_summaries_enabled,
)
from app.observability.metrics import (
    GATEWAY_LLM_PROVIDER_REQUESTS_TOTAL,
    GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL,
    GATEWAY_LLM_REASONING_SUMMARY_UNAVAILABLE_TOTAL,
    GATEWAY_STREAM_SESSIONS_ACTIVE,
)
from app.resilience.rate_limit import rate_limiter
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store

router = APIRouter()
logger = structlog.get_logger()

PROVIDER_CREDENTIAL_BACKEND_UNAVAILABLE = "Provider credential backend unavailable"
PROVIDER_CREDENTIALS_NOT_CONFIGURED = "Provider credentials are not configured"
PROVIDER_NOT_SUPPORTED = "Provider is not supported"
LIVE_TOOL_RESULTS_MARKER = "Live tool results:"


def _is_missing_secret_error(exc: Exception) -> bool:
    return isinstance(exc, SecretNotFoundError)


def _schema_allows_empty_arguments(schema: dict[str, Any]) -> bool:
    required = schema.get("required")
    return not isinstance(required, list) or len(required) == 0


def _request_matches_claim_scope(req: NormalizedLLMRequest, claims: TokenClaims) -> bool:
    if (
        req.run_id != claims.run_id
        or req.workspace_id != claims.workspace_id
        or req.session_id != claims.session_id
        or req.scope.type != claims.scope.type
    ):
        return False

    if claims.scope.type == "workspace":
        return (
            req.workflow_id == claims.workflow_id
            and req.workflow_run_id == claims.workflow_run_id
            and req.workflow_session_id == claims.workflow_session_id
            and req.workflow_step_id == claims.workflow_step_id
            and req.agent_id == claims.agent_id
            and req.agent_version == claims.agent_version
            and req.trigger_id == claims.trigger_id
            and req.target_id == claims.target_id
            and req.target_type == claims.target_type
        )

    return req.target_id == claims.target_id and req.target_type == claims.target_type


def _domain_filters(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    filters = config.get("domainFilters") or {}
    if not isinstance(filters, dict):
        return [], []
    allowed = filters.get("allowedDomains") or []
    blocked = filters.get("blockedDomains") or []
    return (
        list(allowed) if isinstance(allowed, list) else [],
        list(blocked) if isinstance(blocked, list) else [],
    )


def _validate_native_tools(req: NormalizedLLMRequest, claims: TokenClaims) -> None:
    if not req.native_tools:
        return
    allowed_by_id = {
        tool.id: tool.config
        for tool in claims.permissions.allowed_native_tools
    }
    disallowed: list[str] = []
    for tool in req.native_tools:
        allowed_config = allowed_by_id.get(tool.id)
        if allowed_config is None or tool.config != allowed_config:
            disallowed.append(tool.id)
    if disallowed:
        raise HTTPException(
            status_code=403,
            detail=f"Native tool(s) not allowed for this run: {', '.join(disallowed)}",
        )

    if req.provider == "gemini":
        for tool in req.native_tools:
            if tool.id != "web_search":
                continue
            allowed_domains, blocked_domains = _domain_filters(tool.config)
            if allowed_domains:
                raise HTTPException(
                    status_code=400,
                    detail="Gemini web_search does not support allowedDomains policy",
                )
            if blocked_domains:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Gemini web_search domain filtering is not supported by the "
                        "current Gemini API surface"
                    ),
                )


def _select_deterministic_tool(req: NormalizedLLMRequest) -> str | None:
    if any(LIVE_TOOL_RESULTS_MARKER in message.content for message in req.messages):
        return None
    preferred_tools = (
        "get_host_summary",
        "list_services",
        "list_processes",
        "get_resource",
        "list_pods",
    )
    eligible_tools = {
        tool.name
        for tool in req.tools
        if _schema_allows_empty_arguments(tool.input_schema)
    }
    for tool_name in preferred_tools:
        if tool_name in eligible_tools:
            return tool_name
    return next(iter(eligible_tools), None)


async def _deterministic_dev_events(req: NormalizedLLMRequest):
    if reasoning_summaries_enabled(req):
        GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL.labels(
            provider=req.provider,
            model=req.model,
            status="delta",
        ).inc()
        yield StreamEvent(
            type="reasoning_summary_delta",
            text="Reviewing the request and available context.",
            provider=req.provider,
        ).model_dump_json() + "\n"
        GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL.labels(
            provider=req.provider,
            model=req.model,
            status="completed",
        ).inc()
        yield StreamEvent(
            type="reasoning_summary_completed",
            text="Reviewing the request and available context.",
            provider=req.provider,
        ).model_dump_json() + "\n"

    tool_name = _select_deterministic_tool(req)
    if tool_name:
        yield StreamEvent(
            type="tool_call",
            call_id=f"dev-{tool_name}-1",
            tool=tool_name,
            arguments={},
        ).model_dump_json() + "\n"
        yield StreamEvent(
            type="final",
            usage={"input_tokens": 0, "output_tokens": 0, "tool_calls": 1},
        ).model_dump_json() + "\n"
        return

    yield StreamEvent(
        type="delta",
        text=(
            "Local deterministic response: the requested diagnostic context was "
            "received and summarized."
        ),
    ).model_dump_json() + "\n"
    yield StreamEvent(
        type="final",
        usage={"input_tokens": 0, "output_tokens": 18, "tool_calls": 0},
    ).model_dump_json() + "\n"


@router.post("/generations:stream")
async def stream_generation(
    req: NormalizedLLMRequest,
    claims: TokenClaims = Depends(get_current_claims),
):
    # Audit log: request received
    summaries_enabled = reasoning_summaries_enabled(req)
    logger.info(
        "llm_request_received",
        run_id=req.run_id,
        workspace_id=req.workspace_id,
        provider=req.provider,
        model=req.model,
        reasoning_summary_mode=req.reasoning.summary_mode,
        reasoning_summary_requested=summaries_enabled,
        sub=claims.sub,
    )
    if summaries_enabled:
        logger.info(
            "llm_reasoning_summary_requested",
            run_id=req.run_id,
            workspace_id=req.workspace_id,
            provider=req.provider,
            model=req.model,
            reasoning_summary_mode=req.reasoning.summary_mode,
            reasoning_effort=req.reasoning.effort,
        )

    # Apply rate limit
    if rate_limiter:
        await rate_limiter.check_rate_limit(
            f"llm:{claims.workspace_id}",
            limit=settings.LLM_RATE_LIMIT_PER_WINDOW,
            window=settings.RATE_LIMIT_WINDOW_SECONDS,
        )

    start_time = time.time()

    # Verify claims match request
    if not _request_matches_claim_scope(req, claims):
        logger.warning(
            "llm_request_forbidden",
            run_id=req.run_id,
            workspace_id=req.workspace_id,
            target_id=req.target_id,
            target_type=req.target_type,
            session_id=req.session_id,
            scope_type=req.scope.type,
            workflow_id=req.workflow_id,
            workflow_run_id=req.workflow_run_id,
            workflow_session_id=req.workflow_session_id,
            workflow_step_id=req.workflow_step_id,
            agent_id=req.agent_id,
            agent_version=req.agent_version,
            trigger_id=req.trigger_id,
            claims_run_id=claims.run_id,
            claims_workspace_id=claims.workspace_id,
            claims_target_id=claims.target_id,
            claims_target_type=claims.target_type,
            claims_session_id=claims.session_id,
            claims_scope_type=claims.scope.type,
            claims_workflow_id=claims.workflow_id,
            claims_workflow_run_id=claims.workflow_run_id,
            claims_workflow_session_id=claims.workflow_session_id,
            claims_workflow_step_id=claims.workflow_step_id,
            claims_agent_id=claims.agent_id,
            claims_agent_version=claims.agent_version,
            claims_trigger_id=claims.trigger_id,
        )
        raise HTTPException(status_code=403, detail="Scope mismatch between token and request")

    if not is_provider_enabled(req.provider):
        raise HTTPException(status_code=400, detail=f"Provider '{req.provider}' is disabled")

    if claims.permissions.allowed_providers:
        allowed_providers = {
            normalize_provider_name(provider)
            for provider in claims.permissions.allowed_providers
        }
        if req.provider not in allowed_providers:
            raise HTTPException(
                status_code=403,
                detail=f"Provider '{req.provider}' is not allowed for this run",
            )

    if claims.permissions.allowed_models and req.model not in claims.permissions.allowed_models:
        raise HTTPException(
            status_code=403,
            detail=f"Model '{req.model}' is not allowed for this run",
        )

    if (
        req.max_output_tokens is not None
        and claims.permissions.max_output_tokens is not None
        and req.max_output_tokens > claims.permissions.max_output_tokens
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "max_output_tokens exceeds token permission limit "
                f"({req.max_output_tokens} > {claims.permissions.max_output_tokens})"
            ),
        )

    if req.tools:
        requested_tool_names = [tool.name for tool in req.tools]
        disallowed = disallowed_tools(
            requested_tool_names,
            claims.permissions.allowed_tools,
        )
        if disallowed:
            raise HTTPException(
                status_code=403,
                detail=f"Tool(s) not allowed for this run: {', '.join(disallowed)}",
            )

    _validate_native_tools(req, claims)

    if settings.LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES:
        logger.info(
            "llm_deterministic_dev_response_enabled",
            run_id=req.run_id,
            workspace_id=req.workspace_id,
            target_id=req.target_id,
            target_type=req.target_type,
            scope_type=req.scope.type,
            workflow_id=req.workflow_id,
            workflow_run_id=req.workflow_run_id,
            workflow_step_id=req.workflow_step_id,
            agent_id=req.agent_id,
            agent_version=req.agent_version,
            trigger_id=req.trigger_id,
            provider=req.provider,
            model=req.model,
        )
        return StreamingResponse(
            _deterministic_dev_events(req),
            media_type="application/x-ndjson",
        )

    # Resolve workspace-owned provider credentials. Target-scoped secrets are
    # reserved for MCP/tool auth, not LLM provider keys.
    secret_scope = {
        "workspace_id": req.workspace_id,
    }
    secret_names = [f"{req.provider}_api_key"]

    api_key = None
    last_secret_error: Exception | None = None
    for secret_name in secret_names:
        try:
            resolved_api_key = await secret_store.get_secret(secret_name, secret_scope)
            if resolved_api_key and resolved_api_key.strip():
                api_key = resolved_api_key.strip()
                break
        except Exception as e:
            if _is_missing_secret_error(e):
                continue
            last_secret_error = e
            logger.warning(
                "provider_secret_lookup_failed",
                workspace_id=req.workspace_id,
                target_id=req.target_id,
                provider=req.provider,
                secret_name=secret_name,
                error=str(e),
            )

    if not api_key:
        if last_secret_error:
            raise HTTPException(
                status_code=503,
                detail=PROVIDER_CREDENTIAL_BACKEND_UNAVAILABLE,
            ) from last_secret_error
        logger.warning(
            "provider_secret_not_configured",
            workspace_id=req.workspace_id,
            target_id=req.target_id,
            provider=req.provider,
            secret_names=secret_names,
        )
        raise HTTPException(
            status_code=500,
            detail=PROVIDER_CREDENTIALS_NOT_CONFIGURED,
        )

    try:
        adapter = get_adapter(req.provider)
    except ValueError as exc:
        logger.warning(
            "provider_adapter_unavailable",
            provider=req.provider,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=PROVIDER_NOT_SUPPORTED) from exc

    async def event_generator():
        GATEWAY_STREAM_SESSIONS_ACTIVE.inc()
        saw_error = False
        try:
            async for event in adapter.stream(req, api_key):
                if event.type == "error":
                    saw_error = True
                if event.type == "reasoning_summary_delta":
                    GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL.labels(
                        provider=event.provider or req.provider,
                        model=req.model,
                        status="delta",
                    ).inc()
                elif event.type == "reasoning_summary_completed":
                    GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL.labels(
                        provider=event.provider or req.provider,
                        model=req.model,
                        status="completed",
                    ).inc()
                elif event.type == "reasoning_summary_unavailable":
                    reason = event.reason or "provider_omitted"
                    GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL.labels(
                        provider=event.provider or req.provider,
                        model=req.model,
                        status="unavailable",
                    ).inc()
                    GATEWAY_LLM_REASONING_SUMMARY_UNAVAILABLE_TOTAL.labels(
                        provider=event.provider or req.provider,
                        model=req.model,
                        reason=reason,
                    ).inc()
                    logger.info(
                        "llm_reasoning_summary_unavailable",
                        run_id=req.run_id,
                        workspace_id=req.workspace_id,
                        provider=event.provider or req.provider,
                        model=req.model,
                        reason=reason,
                    )
                yield event.model_dump_json() + "\n"
        except Exception:
            saw_error = True
            raise
        finally:
            GATEWAY_STREAM_SESSIONS_ACTIVE.dec()
            logger.info(
                "llm_stream_completed",
                run_id=req.run_id,
                workspace_id=req.workspace_id,
                provider=req.provider,
                model=req.model,
                status="error" if saw_error else "success",
                duration_ms=(time.time() - start_time) * 1000,
            )
            GATEWAY_LLM_PROVIDER_REQUESTS_TOTAL.labels(
                provider=req.provider,
                model=req.model,
                status="error" if saw_error else "success",
            ).inc()

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")
