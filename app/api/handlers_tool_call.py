import hashlib
import time
from typing import Any, Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel, Field, model_validator

from app.api.tool_result_normalization import ToolCallResponse, _json_size, _normalize_tool_response
from app.auth.claims import TokenClaims
from app.auth.jwt_validator import TokenContext, get_current_token_context
from app.auth.tool_permissions import is_tool_permitted
from app.config.settings import settings
from app.errors.codes import ErrorCode
from app.examples import EXAMPLE_RUN_ID, EXAMPLE_TARGET_ID, EXAMPLE_WORKSPACE_ID
from app.internal_model_tools import is_reserved_internal_tool_name
from app.internal_transport import post_builtin_mcp_tool
from app.mcp.header_policy import validate_auth_header_value
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.transports.http_transport import McpToolTransportError, mcp_transport
from app.observability.metrics import (
    GATEWAY_TOOL_CALL_LATENCY_MS,
    GATEWAY_TOOL_CALLS_TOTAL,
)
from app.resilience.rate_limit import rate_limiter
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store
from app.target_types import KUBERNETES_TARGET_TYPE, TARGET_TYPE_EXAMPLES, TargetType

router = APIRouter()
logger = structlog.get_logger()

MCP_SERVER_DISABLED = "MCP server is disabled for this target"
MCP_SERVER_AUTH_NOT_CONFIGURED = "MCP server authentication is not configured"
MCP_SERVER_AUTH_BACKEND_UNAVAILABLE = "MCP server authentication backend unavailable"
BUILTIN_MCP_BRIDGE_NOT_CONFIGURED = "Builtin MCP bridge is not configured for this target"
TOOL_EXECUTION_FAILED_MESSAGE = "Tool execution failed"
BUILTIN_TARGET_MCP_SERVER_NAME = settings.BUILTIN_TARGET_MCP_SERVER_NAME
BUILTIN_TARGET_MCP_SERVER_URL = settings.BUILTIN_TARGET_MCP_SERVER_URL
WORKFLOW_BUILTIN_TOOL_TIMEOUT_MS = 10000


def _is_missing_secret_error(exc: Exception) -> bool:
    return isinstance(exc, SecretNotFoundError)


def _tool_execution_error_response(exc: Exception, capability: str) -> ToolCallResponse:
    """Return a bounded error and conservatively mark ambiguous write outcomes."""
    is_timeout = isinstance(exc, httpx.TimeoutException)
    write_capable = capability != "read"
    error: dict[str, Any] = {
        "code": ErrorCode.TOOL_TIMEOUT if is_timeout else ErrorCode.TOOL_EXECUTION_FAILED,
        "message": "Tool execution timed out" if is_timeout else TOOL_EXECUTION_FAILED_MESSAGE,
        "retryable": is_timeout and not write_capable,
    }
    if write_capable:
        error["outcome"] = "unknown"
    size = _json_size(error)
    return ToolCallResponse(
        full_result=error,
        model_context=error,
        context_meta={
            "schema_version": "v1",
            "strategy": "error",
            "original_bytes": size,
            "context_bytes": size,
            "truncated": False,
            "omissions": [],
        },
        artifact_eligible=False,
        is_error=True,
    )


def _tool_transport_error_response(
    error: McpToolTransportError, capability: str
) -> ToolCallResponse:
    """Preserve trusted local MCP dispatch state for safe write retry behavior."""
    write_capable = capability != "read"
    result: dict[str, Any] = {
        "code": error.code,
        "message": str(error.get("content", [{}])[0].get("text") or "MCP tool call failed."),
        "retryable": error.retryable
        and (not write_capable or error.dispatch_outcome == "not_started"),
    }
    if write_capable:
        result["outcome"] = error.dispatch_outcome
    size = _json_size(result)
    return ToolCallResponse(
        full_result=result,
        model_context=result,
        context_meta={
            "schema_version": "v1",
            "strategy": "transport_error",
            "original_bytes": size,
            "context_bytes": size,
            "truncated": False,
            "omissions": [],
        },
        artifact_eligible=False,
        is_error=True,
    )


def _mark_unknown_write_contract(response: ToolCallResponse, capability: str) -> ToolCallResponse:
    """A malformed response cannot prove that a dispatched write did not happen."""
    if (
        capability != "read"
        and response.is_error
        and isinstance(response.full_result, dict)
        and response.full_result.get("code") in {
            "TOOL_RESULT_SCHEMA_INVALID",
            "TOOL_RESULT_CONTRACT_INVALID",
        }
    ):
        error = {**response.full_result, "outcome": "unknown", "retryable": False}
        size = _json_size(error)
        response.full_result = error
        response.model_context = error
        response.context_meta = {
            **response.context_meta,
            "original_bytes": size,
            "context_bytes": size,
        }
    return response


class ToolCallRequest(BaseModel):
    class Scope(BaseModel):
        type: Literal["target", "workspace"] = "target"

    run_id: str = Field(examples=[EXAMPLE_RUN_ID])
    workspace_id: str = Field(examples=[EXAMPLE_WORKSPACE_ID])
    scope: Scope = Field(default_factory=Scope)
    target_id: str | None = Field(default=None, examples=[EXAMPLE_TARGET_ID])
    target_type: TargetType | None = Field(default=None, examples=TARGET_TYPE_EXAMPLES)
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_session_id: str | None = None
    workflow_step_id: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
    trigger_id: str | None = None
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)
    tool: str = Field(examples=["get_resource_logs"])
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.scope.type == "target":
            if not self.target_id or not self.target_type:
                raise ValueError("target scope requires target_id and target_type")
            return self

        if self.agent_id and not self.workflow_id:
            if (self.target_id and not self.target_type) or (
                self.target_type and not self.target_id
            ):
                raise ValueError("agent target binding requires both target_id and target_type")
            return self
        missing = [
            name
            for name, value in (
                ("workflow_id", self.workflow_id),
                ("workflow_run_id", self.workflow_run_id),
                ("workflow_session_id", self.workflow_session_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"workspace workflow scope missing required fields: {', '.join(missing)}"
            )
        if (self.target_id and not self.target_type) or (self.target_type and not self.target_id):
            raise ValueError("workflow target binding requires both target_id and target_type")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "run_id": EXAMPLE_RUN_ID,
                "workspace_id": EXAMPLE_WORKSPACE_ID,
                "target_id": EXAMPLE_TARGET_ID,
                "target_type": KUBERNETES_TARGET_TYPE,
                "tool": "get_resource_logs",
                "arguments": {
                    "namespace": "payments",
                    "name": "payments-api-7f95b8f79-x2mhd",
                    "tail_lines": 200,
                },
            }
        }
    }


def _request_matches_claim_scope(req: ToolCallRequest, claims: TokenClaims) -> bool:
    if req.run_id != claims.run_id or req.workspace_id != claims.workspace_id:
        return False
    if req.scope.type != claims.scope.type:
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


@router.post("/tool-call", response_model=ToolCallResponse)
async def execute_tool_call(
    req: ToolCallRequest, token_context: TokenContext = Depends(get_current_token_context)
):
    claims: TokenClaims = token_context.claims
    # Audit log: tool call received
    logger.info(
        "tool_call_received",
        run_id=req.run_id,
        workspace_id=req.workspace_id,
        tool=req.tool,
        sub=claims.sub,
    )

    # Apply rate limit
    if rate_limiter:
        await rate_limiter.check_rate_limit(
            f"tool:{claims.workspace_id}",
            limit=settings.TOOL_RATE_LIMIT_PER_WINDOW,
            window=settings.RATE_LIMIT_WINDOW_SECONDS,
        )

    start_time = time.time()
    # Verify claims match request
    if not _request_matches_claim_scope(req, claims):
        logger.warning(
            "tool_call_forbidden",
            run_id=req.run_id,
            workspace_id=req.workspace_id,
            claims_run_id=claims.run_id,
            claims_workspace_id=claims.workspace_id,
            scope_type=req.scope.type,
            claims_scope_type=claims.scope.type,
            workflow_id=req.workflow_id,
            claims_workflow_id=claims.workflow_id,
            agent_id=req.agent_id,
            claims_agent_id=claims.agent_id,
            agent_version=req.agent_version,
            claims_agent_version=claims.agent_version,
            trigger_id=req.trigger_id,
            claims_trigger_id=claims.trigger_id,
        )
        raise HTTPException(status_code=403, detail="Scope mismatch between token and request")
    if is_reserved_internal_tool_name(req.tool):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Tool {req.tool} is reserved for internal model-only use "
                "and cannot be executed"
            ),
        )
    if not is_tool_permitted(req.tool, claims.permissions.allowed_tools):
        raise HTTPException(
            status_code=403,
            detail=f"Tool {req.tool} is not permitted for this run",
        )

    if claims.scope.type == "workspace":
        tool = await tool_registry.get_tool(
            req.workspace_id,
            "__workspace__",
            req.tool,
            target_type="workspace",
        )
        if not tool:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace tool {req.tool} not found or disabled",
            )
        if tool.input_schema:
            try:
                jsonschema_validate(instance=req.arguments, schema=tool.input_schema)
            except JsonSchemaValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "TOOL_ARGS_INVALID",
                        "message": f"Invalid arguments for tool {req.tool}: {exc.message}",
                    },
                ) from exc
        tool_arguments = dict(req.arguments)
        if claims.permissions.allowed_tool_operations.get(req.tool) == "write":
            if not req.tool_call_id:
                raise HTTPException(status_code=400, detail={
                    "code": "WRITE_IDEMPOTENCY_KEY_REQUIRED",
                    "message": "Write tool calls require a stable tool_call_id",
                })
            tool_arguments["idempotencyKey"] = hashlib.sha256(
                f"{req.run_id}:{req.tool_call_id}:{req.tool}".encode()
            ).hexdigest()
        server = await mcp_server_registry.get_server_by_url(
            req.workspace_id,
            "__workspace__",
            tool.mcp_server_url,
            enabled_only=False,
            target_type="workspace",
        )
        if server is None:
            raise HTTPException(status_code=404, detail="Workspace MCP server not found")
        if not server.enabled:
            raise HTTPException(status_code=403, detail="MCP server is disabled for this workspace")

        is_builtin_tool = (
            tool.source == "builtin"
            and server.server_name == BUILTIN_TARGET_MCP_SERVER_NAME
            and server.server_url == BUILTIN_TARGET_MCP_SERVER_URL
        )
        if tool.source == "builtin" and not is_builtin_tool:
            raise HTTPException(status_code=500, detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED)

        request_headers: dict[str, str]
        if is_builtin_tool:
            request_headers = {"Authorization": f"Bearer {token_context.token}"}
        else:
            request_headers = dict(server.public_headers or {})
            request_headers.update({
                "x-workspace-id": req.workspace_id,
                "x-run-id": req.run_id,
                "x-workflow-run-id": req.workflow_run_id or "",
            })
            if server.auth_type in ("bearer_token", "custom_header"):
                if not server.auth_secret_name:
                    raise HTTPException(status_code=500, detail=MCP_SERVER_AUTH_NOT_CONFIGURED)
                try:
                    secret_value = await secret_store.get_secret(
                        server.auth_secret_name,
                        {
                            "workspace_id": req.workspace_id,
                            "target_id": "__workspace__",
                            "target_type": "workspace",
                        },
                    )
                except SecretNotFoundError as exc:
                    raise HTTPException(
                        status_code=500, detail=MCP_SERVER_AUTH_NOT_CONFIGURED
                    ) from exc
                except Exception as exc:
                    logger.warning(
                        "workspace_tool_call_secret_lookup_failed",
                        workspace_id=req.workspace_id,
                        workflow_run_id=req.workflow_run_id,
                        tool=req.tool,
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=MCP_SERVER_AUTH_BACKEND_UNAVAILABLE,
                    ) from exc
                header_name = server.auth_header_name or "Authorization"
                header_value = f"{server.auth_header_prefix or ''}{secret_value}"
                try:
                    validate_auth_header_value(header_value)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=500, detail=MCP_SERVER_AUTH_NOT_CONFIGURED
                    ) from exc
                request_headers[header_name] = header_value

        try:
            if is_builtin_tool:
                mcp_response = await post_builtin_mcp_tool(
                    tool.mcp_server_url,
                    req.tool,
                    tool_arguments,
                    tool.timeout_ms,
                    request_headers,
                    req.tool_call_id,
                )
            else:
                mcp_response = await mcp_transport.call_tool(
                    tool.mcp_server_url,
                    req.tool,
                    tool_arguments,
                    tool.timeout_ms,
                    request_headers,
                )
            is_error = mcp_response.get("isError") is True
            GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=is_error).inc()
            GATEWAY_TOOL_CALL_LATENCY_MS.labels(tool=req.tool).observe(
                (time.time() - start_time) * 1000
            )
            if isinstance(mcp_response, McpToolTransportError):
                return _tool_transport_error_response(mcp_response, str(tool.capability))
            return _mark_unknown_write_contract(
                _normalize_tool_response(
                    mcp_response,
                    trusted_builtin=is_builtin_tool and req.target_type == KUBERNETES_TARGET_TYPE,
                    output_schema=tool.output_schema,
                    artifact_policy=getattr(tool, "artifact_policy", "never"),
                    expected_tool=req.tool,
                ),
                str(tool.capability),
            )
        except Exception as exc:
            GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=True).inc()
            logger.warning(
                "workflow_tool_call_execution_failed",
                workspace_id=req.workspace_id,
                workflow_id=req.workflow_id,
                workflow_run_id=req.workflow_run_id,
                tool=req.tool,
                server_name=server.server_name,
            )
            return _tool_execution_error_response(exc, str(tool.capability))

    # Resolve tool from registry
    tool = await tool_registry.get_tool(
        req.workspace_id,
        req.target_id,
        req.tool,
        target_type=req.target_type,
    )
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool {req.tool} not found or disabled")

    if tool.input_schema:
        try:
            jsonschema_validate(instance=req.arguments, schema=tool.input_schema)
        except JsonSchemaValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "TOOL_ARGS_INVALID",
                    "message": f"Invalid arguments for tool {req.tool}: {exc.message}",
                },
            ) from exc

    server = await mcp_server_registry.get_server_by_url(
        req.workspace_id,
        req.target_id,
        tool.mcp_server_url,
        enabled_only=False,
        target_type=req.target_type,
    )
    if server and not server.enabled:
        raise HTTPException(
            status_code=403,
            detail=MCP_SERVER_DISABLED,
        )

    is_builtin_tool = (
        tool.source == "builtin"
        and server is not None
        and server.server_name == BUILTIN_TARGET_MCP_SERVER_NAME
        and server.server_url == BUILTIN_TARGET_MCP_SERVER_URL
        and tool.mcp_server_url == BUILTIN_TARGET_MCP_SERVER_URL
    )
    if tool.source == "builtin" and not is_builtin_tool:
        logger.warning(
            "tool_call_builtin_bridge_misconfigured",
            workspace_id=req.workspace_id,
            target_id=req.target_id,
            target_type=req.target_type,
            tool=req.tool,
            mcp_server_url=tool.mcp_server_url,
            server_name=server.server_name if server else None,
            server_url=server.server_url if server else None,
        )
        raise HTTPException(status_code=500, detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED)

    if is_builtin_tool:
        request_headers: dict[str, str] = {
            "Authorization": f"Bearer {token_context.token}",
        }
    else:
        request_headers = dict(server.public_headers or {}) if server else {}
        request_headers.update(
            {
                "x-workspace-id": req.workspace_id,
                "x-target-id": req.target_id,
                "x-target-type": req.target_type,
                "x-run-id": req.run_id,
            }
        )

    if not is_builtin_tool and server and server.auth_type in ("bearer_token", "custom_header"):
        if not server.auth_secret_name:
            logger.warning(
                "tool_call_auth_secret_name_missing",
                workspace_id=req.workspace_id,
                target_id=req.target_id,
                tool=req.tool,
                server_name=server.server_name,
            )
            raise HTTPException(
                status_code=500,
                detail=MCP_SERVER_AUTH_NOT_CONFIGURED,
            )

        try:
            secret_value = await secret_store.get_secret(
                server.auth_secret_name,
                {
                    "workspace_id": req.workspace_id,
                    "target_id": req.target_id,
                    "target_type": req.target_type,
                },
            )
            if not secret_value:
                logger.warning(
                    "tool_call_auth_secret_empty",
                    workspace_id=req.workspace_id,
                    target_id=req.target_id,
                    tool=req.tool,
                    server_name=server.server_name,
                    secret_name=server.auth_secret_name,
                )
                raise HTTPException(
                    status_code=500,
                    detail=MCP_SERVER_AUTH_NOT_CONFIGURED,
                )
            header_name = server.auth_header_name or "Authorization"
            prefix = server.auth_header_prefix or ""
            header_value = f"{prefix}{secret_value}"
            try:
                validate_auth_header_value(header_value)
            except ValueError as exc:
                logger.warning(
                    "tool_call_auth_secret_invalid_header_value",
                    workspace_id=req.workspace_id,
                    target_id=req.target_id,
                    tool=req.tool,
                    server_name=server.server_name,
                    secret_name=server.auth_secret_name,
                    error=str(exc),
                )
                raise HTTPException(
                    status_code=500,
                    detail=MCP_SERVER_AUTH_NOT_CONFIGURED,
                ) from exc
            request_headers[header_name] = header_value
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            if _is_missing_secret_error(exc):
                logger.warning(
                    "tool_call_auth_secret_missing",
                    workspace_id=req.workspace_id,
                    target_id=req.target_id,
                    tool=req.tool,
                    server_name=server.server_name,
                    secret_name=server.auth_secret_name,
                )
                raise HTTPException(
                    status_code=500,
                    detail=MCP_SERVER_AUTH_NOT_CONFIGURED,
                ) from exc
            logger.warning(
                "tool_call_secret_lookup_failed",
                workspace_id=req.workspace_id,
                target_id=req.target_id,
                tool=req.tool,
                server_name=server.server_name,
                secret_name=server.auth_secret_name,
                error=str(exc),
            )
            raise HTTPException(
                status_code=503,
                detail=MCP_SERVER_AUTH_BACKEND_UNAVAILABLE,
            ) from exc

    # Execute tool call
    try:
        if is_builtin_tool:
            mcp_response = await post_builtin_mcp_tool(
                tool.mcp_server_url,
                req.tool,
                req.arguments,
                tool.timeout_ms,
                request_headers,
                req.tool_call_id,
            )
        else:
            mcp_response = await mcp_transport.call_tool(
                tool.mcp_server_url,
                req.tool,
                req.arguments,
                tool.timeout_ms,
                request_headers,
            )

        is_error = mcp_response.get("isError") is True
        GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=is_error).inc()
        GATEWAY_TOOL_CALL_LATENCY_MS.labels(tool=req.tool).observe(
            (time.time() - start_time) * 1000
        )
        if isinstance(mcp_response, McpToolTransportError):
            return _tool_transport_error_response(mcp_response, str(tool.capability))
        return _mark_unknown_write_contract(
            _normalize_tool_response(
                mcp_response,
                trusted_builtin=is_builtin_tool and req.target_type == KUBERNETES_TARGET_TYPE,
                output_schema=tool.output_schema,
                artifact_policy=getattr(tool, "artifact_policy", "never"),
                expected_tool=req.tool,
            ),
            str(tool.capability),
        )
    except Exception as e:
        GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=True).inc()
        logger.warning(
            "tool_call_execution_failed",
            workspace_id=req.workspace_id,
            target_id=req.target_id,
            tool=req.tool,
            error=str(e),
            exc_info=True,
        )
        return _tool_execution_error_response(e, str(tool.capability))
