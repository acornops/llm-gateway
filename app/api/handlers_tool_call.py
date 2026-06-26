import time
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel, Field, model_validator

from app.auth.claims import TokenClaims
from app.auth.jwt_validator import TokenContext, get_current_token_context
from app.auth.tool_permissions import is_tool_permitted
from app.config.settings import settings
from app.errors.codes import ErrorCode
from app.examples import EXAMPLE_RUN_ID, EXAMPLE_TARGET_ID, EXAMPLE_WORKSPACE_ID
from app.internal_transport import post_builtin_mcp_tool
from app.mcp.header_policy import validate_auth_header_value
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.transports.http_transport import mcp_transport
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
BUILTIN_MCP_SERVER_NAME = settings.BUILTIN_MCP_SERVER_NAME
BUILTIN_MCP_SERVER_URL = settings.BUILTIN_MCP_SERVER_URL
WORKFLOW_BUILTIN_TOOL_TIMEOUT_MS = 10000


def _is_missing_secret_error(exc: Exception) -> bool:
    return isinstance(exc, SecretNotFoundError)


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
    tool: str = Field(examples=["get_resource_logs"])
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.scope.type == "target":
            if not self.target_id or not self.target_type:
                raise ValueError("target scope requires target_id and target_type")
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


class ToolCallResponse(BaseModel):
    result: Any
    is_error: bool


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
        )
        raise HTTPException(status_code=403, detail="Scope mismatch between token and request")
    if not is_tool_permitted(req.tool, claims.permissions.allowed_tools):
        raise HTTPException(
            status_code=403,
            detail=f"Tool {req.tool} is not permitted for this run",
        )

    if claims.scope.type == "workspace":
        try:
            mcp_response = await post_builtin_mcp_tool(
                BUILTIN_MCP_SERVER_URL,
                req.tool,
                req.arguments,
                WORKFLOW_BUILTIN_TOOL_TIMEOUT_MS,
                {"Authorization": f"Bearer {token_context.token}"},
            )
            is_error = mcp_response.get("isError", False)
            GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=is_error).inc()
            GATEWAY_TOOL_CALL_LATENCY_MS.labels(tool=req.tool).observe(
                (time.time() - start_time) * 1000
            )
            return ToolCallResponse(result=mcp_response.get("content", []), is_error=is_error)
        except Exception as e:
            GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=True).inc()
            logger.warning(
                "workflow_tool_call_execution_failed",
                workspace_id=req.workspace_id,
                workflow_id=req.workflow_id,
                workflow_run_id=req.workflow_run_id,
                tool=req.tool,
                error=str(e),
                exc_info=True,
            )
            return ToolCallResponse(
                result={
                    "code": ErrorCode.TOOL_EXECUTION_FAILED,
                    "message": TOOL_EXECUTION_FAILED_MESSAGE,
                },
                is_error=True,
            )

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
        and server.server_name == BUILTIN_MCP_SERVER_NAME
        and server.server_url == BUILTIN_MCP_SERVER_URL
        and tool.mcp_server_url == BUILTIN_MCP_SERVER_URL
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
            )
        else:
            mcp_response = await mcp_transport.call_tool(
                tool.mcp_server_url,
                req.tool,
                req.arguments,
                tool.timeout_ms,
                request_headers,
            )

        is_error = mcp_response.get("isError", False)
        GATEWAY_TOOL_CALLS_TOTAL.labels(tool=req.tool, is_error=is_error).inc()
        GATEWAY_TOOL_CALL_LATENCY_MS.labels(tool=req.tool).observe(
            (time.time() - start_time) * 1000
        )
        return ToolCallResponse(result=mcp_response.get("content", []), is_error=is_error)
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
        return ToolCallResponse(
            result={
                "code": ErrorCode.TOOL_EXECUTION_FAILED,
                "message": TOOL_EXECUTION_FAILED_MESSAGE,
            },
            is_error=True,
        )
