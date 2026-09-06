import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate

from app.api.mcp_lifecycle_guard import guarded_server_operation
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
from app.execution_capacity import GENERATION_HEADER, OWNER_HEADER, execution_authority
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


def _builtin_dispatch_url(server) -> str:
    """Return the pinned bridge only for an exact secret-free built-in row."""

    canonical_url = settings.BUILTIN_TARGET_MCP_SERVER_URL
    if any(
        (
            server.server_url != canonical_url,
            getattr(server, "auth_type", "none") != "none",
            getattr(server, "credential_mode", "none") != "none",
            getattr(server, "auth_header_name", None) is not None,
            getattr(server, "auth_header_prefix", None) is not None,
            bool(getattr(server, "public_headers", None)),
            bool(getattr(server, "credential_transitioning", False)),
        )
    ):
        logger.error(
            "tool_call_builtin_bridge_trust_invalid",
            workspace_id=getattr(server, "workspace_id", None),
            server_id=str(getattr(server, "id", "")),
            server_url=loggable_mcp_server_origin(server.server_url),
        )
        raise HTTPException(
            status_code=500,
            detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED,
        )
    return canonical_url


def _enforce_reviewed_authority(tool, server, req: ToolCallRequest, claims: TokenClaims) -> bool:
    if tool.source != "builtin" and getattr(tool, "review_state", "pending") != "approved":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MCP_TOOL_NOT_APPROVED",
                "message": "The MCP tool has not been approved for this Agent.",
            },
        )
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
        await validate_and_claim_approval_receipt(
            req.approval_receipt or "", req, dict(req.arguments)
        )
    except ApprovalReceiptError as exc:
        raise HTTPException(
            status_code=409
            if exc.code in {"MCP_APPROVAL_RECEIPT_EXPIRED", "MCP_APPROVAL_RECEIPT_REPLAYED"}
            else 403,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


def _enforce_current_tool_operation(
    tool,
    req: ToolCallRequest,
    claims: TokenClaims,
) -> None:
    current_operation = "read" if tool.capability == "read" else "write"
    token_operation = claims.permissions.allowed_tool_operations.get(req.tool)
    if (
        current_operation == "write"
        and token_operation != "write"
        or current_operation == "read"
        and token_operation not in (None, "read")
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_TOOL_AUTHORITY_CHANGED",
                "message": "The MCP tool authority changed after this run was authorized.",
                "serverId": str(tool.server_id),
                "toolName": tool.tool_name,
            },
        )
    if current_operation == "write" and not req.tool_call_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WRITE_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Write tool calls require a stable tool_call_id",
            },
        )


@router.post("/tool-call", response_model=ToolCallResponse)
async def execute_tool_call(
    req: ToolCallRequest, token_context: TokenContext = Depends(get_current_token_context),
    request: Request = None
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
            trigger_id=req.trigger_id,
            claims_trigger_id=claims.trigger_id,
        )
        raise HTTPException(status_code=403, detail="Scope mismatch between token and request")
    await execution_authority.authorize(claims)
    authority_headers = dict(request.headers) if request else {}
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
    dispatch_target_id = req.target_id
    dispatch_target_type = req.target_type
    target_tool_arguments = dict(req.arguments)
    agent_server = None
    if claims.agent_id:
        agent_server = await mcp_server_registry.get_server(
            req.workspace_id,
            claims.agent_id,
            req.tool_ref.server_id,
            scope_type="agent",
        )
    if agent_server is not None:
        server = agent_server
        tool_arguments = dict(req.arguments)
        async with guarded_server_operation(
            req.workspace_id,
            str(server.id),
            expected_credential_epoch=int(getattr(server, "credential_epoch", 1) or 1),
        ):
            current_server = await mcp_server_registry.get_server(
                req.workspace_id,
                claims.agent_id,
                str(server.id),
                scope_type="agent",
            )
            if current_server is None:
                raise HTTPException(status_code=404, detail="Agent MCP server not found")
            current_tool = await resolve_registered_tool(
                req,
                destination_id=claims.agent_id,
                scope_type="agent",
                registry=tool_registry,
                fresh=True,
            )
            if current_tool is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent MCP tool {req.tool} not found or disabled",
                )
            if not tool_ref_is_permitted(current_tool, req, claims):
                raise HTTPException(
                    status_code=403,
                    detail=f"Tool {req.tool} is not permitted for this run",
                )
            if str(current_tool.server_id) != str(current_server.id):
                raise HTTPException(status_code=404, detail="Agent MCP server not found")
            if not current_server.enabled:
                raise HTTPException(
                    status_code=403,
                    detail="MCP server is disabled for this Agent",
                )
            _enforce_current_tool_operation(current_tool, req, claims)
            if current_tool.input_schema:
                try:
                    jsonschema_validate(
                        instance=tool_arguments,
                        schema=current_tool.input_schema,
                    )
                except JsonSchemaValidationError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "TOOL_ARGS_INVALID",
                            "message": f"Invalid arguments for tool {req.tool}: {exc.message}",
                        },
                    ) from exc
            tool = current_tool
            server = current_server
            dispatch_server_url = server.server_url
            if tool.mcp_server_url != dispatch_server_url:
                logger.warning(
                    "mcp_tool_server_url_mismatch",
                    workspace_id=req.workspace_id,
                    scope_type="agent",
                    destination_id=claims.agent_id,
                    server_id=str(server.id),
                    tool_name=tool.tool_name,
                    tool_url=loggable_mcp_server_origin(tool.mcp_server_url),
                    server_url=loggable_mcp_server_origin(dispatch_server_url),
                )
            is_builtin_tool = (
                tool.source == "builtin"
                and getattr(server, "provenance_type", "manual") == "builtin"
            )
            if (tool.source == "builtin") != (
                getattr(server, "provenance_type", "manual") == "builtin"
            ):
                raise HTTPException(
                    status_code=500,
                    detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED,
                )
            if is_builtin_tool:
                dispatch_server_url = _builtin_dispatch_url(server)
            else:
                require_remote_mcp_enabled()
            await _authorize_tool_dispatch(tool, server, req, claims)
            if is_builtin_tool:
                request_headers: dict[str, str] = {
                    "Authorization": f"Bearer {token_context.token}"
                }
            else:
                platform_headers = {
                    "x-workspace-id": req.workspace_id,
                    "x-agent-id": claims.agent_id,
                    "x-run-id": req.run_id,
                }
                if req.execution_id:
                    platform_headers["x-workflow-execution-id"] = req.execution_id
                request_headers = await connection_request_headers(
                    server, claims, tool.tool_name, platform_headers=platform_headers
                )

        try:
            async with execution_authority.operation(
                claims, authority_headers, tool.timeout_ms + 5000,
            ):
                if is_builtin_tool:
                    request_headers.update({
                        key: authority_headers[key]
                        for key in (OWNER_HEADER, GENERATION_HEADER) if key in authority_headers
                    })
                if is_builtin_tool:
                    mcp_response = await post_builtin_mcp_tool(
                        dispatch_server_url,
                        tool.tool_name,
                        tool_arguments,
                        tool.timeout_ms,
                        request_headers,
                        req.tool_call_id,
                        tool_ref=req.tool_ref.model_dump() if req.tool_ref else None,
                    )
                else:
                    mcp_response = await mcp_transport.call_tool(
                        dispatch_server_url,
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
                    await mark_connection_error(
                        server,
                        claims,
                        auth_error=mcp_response.auth_error,
                        required_scopes=mcp_response.required_scopes,
                        expected_connection_id=getattr(
                            request_headers,
                            "connection_id",
                            None,
                        ),
                        expected_credential_fingerprint=getattr(
                            request_headers,
                            "credential_fingerprint",
                            None,
                        ),
                    )
                return _tool_transport_error_response(mcp_response, str(tool.capability))
            return _mark_unknown_write_contract(
                _normalize_tool_response(
                    mcp_response,
                    trusted_builtin=False,
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
            execution_context = (
                {
                    "workflow_id": req.workflow_id,
                    "execution_id": req.execution_id,
                    "executor_role": req.executor_role,
                }
                if claims.scope.type == "workspace"
                else {"agent_id": claims.agent_id}
            )
            logger.warning(
                "scoped_mcp_tool_call_execution_failed",
                workspace_id=req.workspace_id,
                tool=req.tool,
                server_name=server.server_name,
                **execution_context,
            )
            return _tool_execution_error_response(exc, str(tool.capability))

    if claims.scope.type in {"workspace", "agent_chat"} and not (
        dispatch_target_id and dispatch_target_type
    ):
        raise HTTPException(
            status_code=404,
            detail=f"Agent MCP tool {req.tool} not found or disabled",
        )

    server = await mcp_server_registry.get_server(
        req.workspace_id,
        dispatch_target_id,
        req.tool_ref.server_id,
        target_type=dispatch_target_type,
        scope_type="target",
    )
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    async with guarded_server_operation(
        req.workspace_id,
        str(server.id),
        expected_credential_epoch=int(getattr(server, "credential_epoch", 1) or 1),
    ):
        current_server = await mcp_server_registry.get_server(
            req.workspace_id,
            dispatch_target_id,
            str(server.id),
            target_type=dispatch_target_type,
            scope_type="target",
        )
        if current_server is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        current_tool = await resolve_registered_tool(
            req,
            destination_id=dispatch_target_id,
            target_type=dispatch_target_type,
            registry=tool_registry,
            fresh=True,
        )
        if current_tool is None:
            raise HTTPException(
                status_code=404,
                detail=f"Tool {req.tool} not found or disabled",
            )
        if not tool_ref_is_permitted(current_tool, req, claims):
            raise HTTPException(
                status_code=403,
                detail=f"Tool {req.tool} is not permitted for this run",
            )
        if str(current_tool.server_id) != str(current_server.id):
            raise HTTPException(status_code=404, detail="MCP server not found")
        if not current_server.enabled:
            raise HTTPException(status_code=403, detail=MCP_SERVER_DISABLED)
        _enforce_current_tool_operation(current_tool, req, claims)
        if current_tool.input_schema:
            try:
                jsonschema_validate(
                    instance=target_tool_arguments,
                    schema=current_tool.input_schema,
                )
            except JsonSchemaValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "TOOL_ARGS_INVALID",
                        "message": f"Invalid arguments for tool {req.tool}: {exc.message}",
                    },
                ) from exc
        tool = current_tool
        server = current_server
        dispatch_server_url = server.server_url
        if tool.mcp_server_url != dispatch_server_url:
            logger.warning(
                "mcp_tool_server_url_mismatch",
                workspace_id=req.workspace_id,
                scope_type="target",
                destination_id=dispatch_target_id,
                target_type=dispatch_target_type,
                server_id=str(server.id),
                tool_name=tool.tool_name,
                tool_url=loggable_mcp_server_origin(tool.mcp_server_url),
                server_url=loggable_mcp_server_origin(dispatch_server_url),
            )
        is_builtin_tool = (
            tool.source == "builtin"
            and getattr(server, "provenance_type", "manual") == "builtin"
        )
        if (tool.source == "builtin") != (
            getattr(server, "provenance_type", "manual") == "builtin"
        ):
            logger.warning(
                "tool_call_builtin_bridge_misconfigured",
                workspace_id=req.workspace_id,
                target_id=dispatch_target_id,
                target_type=dispatch_target_type,
                tool=req.tool,
                mcp_server_url=loggable_mcp_server_origin(tool.mcp_server_url),
                server_name=server.server_name,
                server_url=loggable_mcp_server_origin(server.server_url),
            )
            raise HTTPException(
                status_code=500,
                detail=BUILTIN_MCP_BRIDGE_NOT_CONFIGURED,
            )
        if is_builtin_tool:
            dispatch_server_url = _builtin_dispatch_url(server)
        else:
            require_remote_mcp_enabled()
        await _authorize_tool_dispatch(tool, server, req, claims)
        if is_builtin_tool:
            request_headers: dict[str, str] = {
                "Authorization": f"Bearer {token_context.token}",
            }
        else:
            platform_headers = {
                "x-workspace-id": req.workspace_id,
                "x-target-id": dispatch_target_id,
                "x-target-type": dispatch_target_type,
                "x-run-id": req.run_id,
            }
            request_headers = await connection_request_headers(
                server, claims, tool.tool_name, platform_headers=platform_headers
            )

    # Execute tool call
    try:
        async with execution_authority.operation(
                claims, authority_headers, tool.timeout_ms + 5000,
            ):
            if is_builtin_tool:
                request_headers.update({
                        key: authority_headers[key]
                        for key in (OWNER_HEADER, GENERATION_HEADER) if key in authority_headers
                    })
            if is_builtin_tool:
                mcp_response = await post_builtin_mcp_tool(
                    dispatch_server_url,
                    tool.tool_name,
                    target_tool_arguments,
                    tool.timeout_ms,
                    request_headers,
                    req.tool_call_id,
                    target_id=dispatch_target_id,
                    target_type=dispatch_target_type,
                    tool_ref=req.tool_ref.model_dump() if req.tool_ref else None,
                )
            else:
                mcp_response = await mcp_transport.call_tool(
                    dispatch_server_url,
                    tool.tool_name,
                    target_tool_arguments,
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
                await mark_connection_error(
                    server,
                    claims,
                    auth_error=mcp_response.auth_error,
                    required_scopes=mcp_response.required_scopes,
                    expected_connection_id=getattr(
                        request_headers,
                        "connection_id",
                        None,
                    ),
                    expected_credential_fingerprint=getattr(
                        request_headers,
                        "credential_fingerprint",
                        None,
                    ),
                )
            return _tool_transport_error_response(mcp_response, str(tool.capability))
        return _mark_unknown_write_contract(
            _normalize_tool_response(
                mcp_response,
                trusted_builtin=(
                    is_builtin_tool and dispatch_target_type == KUBERNETES_TARGET_TYPE
                ),
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
            target_id=dispatch_target_id,
            tool=req.tool,
            error=str(e),
            exc_info=True,
        )
        return _tool_execution_error_response(e, str(tool.capability))
