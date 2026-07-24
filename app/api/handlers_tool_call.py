import hashlib
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate

from app.api.mcp_runtime_auth import (
    connection_request_headers,
    mark_connection_error,
)
from app.api.tool_call_contract import (
    ToolCallRequest,
    request_matches_claim_scope,
    resolve_registered_tool,
    tool_ref_is_permitted,
)
from app.api.tool_call_errors import (
    _mark_unknown_write_contract,
    _tool_execution_error_response,
    _tool_transport_error_response,
)
from app.api.tool_result_normalization import ToolCallResponse, _normalize_tool_response
from app.auth.claims import TokenClaims
from app.auth.jwt_validator import TokenContext, get_current_token_context
from app.config.settings import settings
from app.internal_model_tools import is_reserved_internal_tool_name
from app.internal_transport import post_builtin_mcp_tool
from app.mcp.approval_receipts import ApprovalReceiptError, validate_and_claim_approval_receipt
from app.mcp.logging import loggable_mcp_server_origin
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.mcp.transports.http_transport import McpToolTransportError, mcp_transport
from app.observability.metrics import (
    GATEWAY_MCP_SCOPED_INVOCATIONS_TOTAL,
    GATEWAY_TOOL_CALL_LATENCY_MS,
    GATEWAY_TOOL_CALLS_TOTAL,
)
from app.resilience.rate_limit import rate_limiter
from app.target_types import KUBERNETES_TARGET_TYPE

router = APIRouter()
logger = structlog.get_logger()


MCP_SERVER_DISABLED = "MCP server is disabled for this target"
MCP_SERVER_AUTH_NOT_CONFIGURED = "MCP server authentication is not configured"
BUILTIN_MCP_BRIDGE_NOT_CONFIGURED = "Builtin MCP bridge is not configured for this target"
WORKFLOW_BUILTIN_TOOL_TIMEOUT_MS = 10000


def _enforce_reviewed_authority(tool, server, req: ToolCallRequest, claims: TokenClaims) -> bool:
    if tool.source != "builtin" and getattr(tool, "review_state", "pending") != "approved":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MCP_TOOL_NOT_APPROVED",
                "message": "The MCP tool has not been approved for this Agent.",
            },
        )
    constraints = getattr(server, "target_constraints", {}) or {}
    allowed_target_types = set(constraints.get("target_types") or [])
    allowed_target_ids = set(constraints.get("target_ids") or [])
    if req.target_type and allowed_target_types and req.target_type not in allowed_target_types:
        raise HTTPException(status_code=403, detail="Tool is not allowed for this target type")
    if req.target_id and allowed_target_ids and req.target_id not in allowed_target_ids:
        raise HTTPException(status_code=403, detail="Tool is not allowed for this target")

    capability = "read" if tool.capability == "read" else "write"
    risk = getattr(tool, "risk_level", "high_risk")
    if claims.permission_mode == "read_only" and capability != "read":
        raise HTTPException(status_code=403, detail="Run permission mode is read only")
    if capability == "read":
        return False
    if (
        claims.permission_mode == "auto_allowed_changes"
        and risk == "non_destructive_write"
        and bool(getattr(tool, "auto_allowed", False))
    ):
        return False
    if req.approval_receipt:
        return True
    raise HTTPException(
        status_code=409,
        detail={
            "code": "MCP_TOOL_APPROVAL_REQUIRED",
            "message": "This change requires a current approval receipt.",
            "serverId": str(tool.server_id),
            "toolName": tool.tool_name,
        },
    )


async def _authorize_tool_dispatch(tool, server, req: ToolCallRequest, claims: TokenClaims) -> None:
    if not _enforce_reviewed_authority(tool, server, req, claims):
        return
    try:
        await validate_and_claim_approval_receipt(req.approval_receipt or "", req)
    except ApprovalReceiptError as exc:
        raise HTTPException(
            status_code=409
            if exc.code in {"MCP_APPROVAL_RECEIPT_EXPIRED", "MCP_APPROVAL_RECEIPT_REPLAYED"}
            else 403,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


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
    if not request_matches_claim_scope(req, claims):
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
                f"Tool {req.tool} is reserved for internal model-only use and cannot be executed"
            ),
        )
    if req.tool_ref is None:
        raise HTTPException(
            status_code=403,
            detail="MCP tool calls require a server-qualified tool_ref",
        )
    agent_tool = None
    if claims.agent_id:
        agent_tool = await resolve_registered_tool(
            req,
            target_id=claims.agent_id,
            target_type="agent",
            registry=tool_registry,
        )
    if agent_tool is not None:
        tool = agent_tool
        if not tool_ref_is_permitted(tool, req, claims):
            raise HTTPException(
                status_code=403, detail=f"Tool {req.tool} is not permitted for this run"
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
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "WRITE_IDEMPOTENCY_KEY_REQUIRED",
                        "message": "Write tool calls require a stable tool_call_id",
                    },
                )
            tool_arguments["idempotencyKey"] = hashlib.sha256(
                f"{req.run_id}:{req.tool_call_id}:{req.tool}".encode()
            ).hexdigest()
        server = (
            await mcp_server_registry.get_server(
                req.workspace_id,
                claims.agent_id,
                str(tool.server_id),
                target_type="agent",
            )
            if tool.server_id
            else await mcp_server_registry.get_server_by_url(
                req.workspace_id,
                claims.agent_id,
                tool.mcp_server_url,
                target_type="agent",
            )
        )
        if server is None:
            raise HTTPException(status_code=404, detail="Agent MCP server not found")
        if not server.enabled:
            raise HTTPException(status_code=403, detail="MCP server is disabled for this Agent")
        is_builtin_tool = (
            tool.source == "builtin" and getattr(server, "provenance_type", "manual") == "builtin"
        )
        if (tool.source == "builtin") != (
            getattr(server, "provenance_type", "manual") == "builtin"
        ):
            raise HTTPException(status_code=500, detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED)

        request_headers: dict[str, str]
        if is_builtin_tool:
            request_headers = {"Authorization": f"Bearer {token_context.token}"}
        else:
            platform_headers = {
                "x-workspace-id": req.workspace_id,
                "x-agent-id": claims.agent_id,
                "x-run-id": req.run_id,
                "x-workflow-execution-id": req.execution_id or "",
            }
            request_headers = await connection_request_headers(
                server, claims, tool.tool_name, platform_headers=platform_headers
            )

        if not is_builtin_tool:
            require_remote_mcp_enabled()

        await _authorize_tool_dispatch(tool, server, req, claims)

        try:
            if is_builtin_tool:
                mcp_response = await post_builtin_mcp_tool(
                    tool.mcp_server_url,
                    tool.tool_name,
                    tool_arguments,
                    tool.timeout_ms,
                    request_headers,
                    req.tool_call_id,
                )
            else:
                mcp_response = await mcp_transport.call_tool(
                    tool.mcp_server_url,
                    tool.tool_name,
                    tool_arguments,
                    tool.timeout_ms,
                    request_headers,
                )
            is_error = mcp_response.get("isError") is True
            GATEWAY_MCP_SCOPED_INVOCATIONS_TOTAL.labels(
                scope_type="agent",
                source="builtin" if is_builtin_tool else "remote",
                outcome="tool_error" if is_error else "success",
            ).inc()
            GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=is_error).inc()
            GATEWAY_TOOL_CALL_LATENCY_MS.labels(tool=req.tool).observe(
                (time.time() - start_time) * 1000
            )
            if isinstance(mcp_response, McpToolTransportError):
                if (
                    mcp_response.code == "MCP_AUTHENTICATION_FAILED"
                    and server.credential_mode != "none"
                ):
                    await mark_connection_error(server, claims)
                return _tool_transport_error_response(mcp_response, str(tool.capability))
            return _mark_unknown_write_contract(
                _normalize_tool_response(
                    mcp_response,
                    trusted_builtin=is_builtin_tool and req.target_type == KUBERNETES_TARGET_TYPE,
                    output_schema=tool.output_schema,
                    artifact_policy=getattr(tool, "artifact_policy", "never"),
                    expected_tool=tool.tool_name,
                ),
                str(tool.capability),
            )
        except Exception as exc:
            GATEWAY_MCP_SCOPED_INVOCATIONS_TOTAL.labels(
                scope_type="agent",
                source="builtin" if is_builtin_tool else "remote",
                outcome="transport_error",
            ).inc()
            GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=True).inc()
            logger.warning(
                "workflow_tool_call_execution_failed",
                workspace_id=req.workspace_id,
                workflow_id=req.workflow_id,
                execution_id=req.execution_id,
                executor_role=req.executor_role,
                tool=req.tool,
                server_name=server.server_name,
            )
            return _tool_execution_error_response(exc, str(tool.capability))

    if claims.scope.type == "workspace":
        raise HTTPException(
            status_code=404,
            detail=f"Agent MCP tool {req.tool} not found or disabled",
        )

    # Resolve tool from registry
    tool = await resolve_registered_tool(
        req,
        target_id=req.target_id,
        target_type=req.target_type,
        registry=tool_registry,
    )
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool {req.tool} not found or disabled")
    if not tool_ref_is_permitted(tool, req, claims):
        raise HTTPException(
            status_code=403, detail=f"Tool {req.tool} is not permitted for this run"
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

    server = (
        await mcp_server_registry.get_server(
            req.workspace_id,
            req.target_id,
            str(tool.server_id),
            target_type=req.target_type,
        )
        if tool.server_id
        else await mcp_server_registry.get_server_by_url(
            req.workspace_id,
            req.target_id,
            tool.mcp_server_url,
            target_type=req.target_type,
        )
    )
    if server and not server.enabled:
        raise HTTPException(
            status_code=403,
            detail=MCP_SERVER_DISABLED,
        )

    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    is_builtin_tool = (
        tool.source == "builtin"
        and server is not None
        and getattr(server, "provenance_type", "manual") == "builtin"
    )
    if (tool.source == "builtin") != (getattr(server, "provenance_type", "manual") == "builtin"):
        logger.warning(
            "tool_call_builtin_bridge_misconfigured",
            workspace_id=req.workspace_id,
            target_id=req.target_id,
            target_type=req.target_type,
            tool=req.tool,
            mcp_server_url=loggable_mcp_server_origin(tool.mcp_server_url),
            server_name=server.server_name if server else None,
            server_url=(loggable_mcp_server_origin(server.server_url) if server else None),
        )
        raise HTTPException(status_code=500, detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED)

    if is_builtin_tool:
        request_headers: dict[str, str] = {
            "Authorization": f"Bearer {token_context.token}",
        }
    else:
        platform_headers = {
            "x-workspace-id": req.workspace_id,
            "x-target-id": req.target_id,
            "x-target-type": req.target_type,
            "x-run-id": req.run_id,
        }
        request_headers = await connection_request_headers(
            server, claims, tool.tool_name, platform_headers=platform_headers
        )

    if not is_builtin_tool:
        require_remote_mcp_enabled()

    await _authorize_tool_dispatch(tool, server, req, claims)

    # Execute tool call
    try:
        if is_builtin_tool:
            mcp_response = await post_builtin_mcp_tool(
                tool.mcp_server_url,
                tool.tool_name,
                req.arguments,
                tool.timeout_ms,
                request_headers,
                req.tool_call_id,
            )
        else:
            mcp_response = await mcp_transport.call_tool(
                tool.mcp_server_url,
                tool.tool_name,
                req.arguments,
                tool.timeout_ms,
                request_headers,
            )

        is_error = mcp_response.get("isError") is True
        GATEWAY_MCP_SCOPED_INVOCATIONS_TOTAL.labels(
            scope_type="target",
            source="builtin" if is_builtin_tool else "remote",
            outcome="tool_error" if is_error else "success",
        ).inc()
        GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=is_error).inc()
        GATEWAY_TOOL_CALL_LATENCY_MS.labels(tool=req.tool).observe(
            (time.time() - start_time) * 1000
        )
        if isinstance(mcp_response, McpToolTransportError):
            if (
                mcp_response.code == "MCP_AUTHENTICATION_FAILED"
                and server
                and server.credential_mode != "none"
            ):
                await mark_connection_error(server, claims)
            return _tool_transport_error_response(mcp_response, str(tool.capability))
        return _mark_unknown_write_contract(
            _normalize_tool_response(
                mcp_response,
                trusted_builtin=is_builtin_tool and req.target_type == KUBERNETES_TARGET_TYPE,
                output_schema=tool.output_schema,
                artifact_policy=getattr(tool, "artifact_policy", "never"),
                expected_tool=tool.tool_name,
            ),
            str(tool.capability),
        )
    except Exception as e:
        GATEWAY_MCP_SCOPED_INVOCATIONS_TOTAL.labels(
            scope_type="target",
            source="builtin" if is_builtin_tool else "remote",
            outcome="transport_error",
        ).inc()
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
