from fastapi import HTTPException

from app.auth.claims import TokenClaims
from app.mcp.connections import mcp_connection_store
from app.mcp.header_policy import validate_auth_header_value
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.observability.metrics import (
    GATEWAY_MCP_READINESS_FAILURES_TOTAL,
    GATEWAY_MCP_RUNTIME_AUTH_REJECTIONS_TOTAL,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store


async def mark_personal_connection_error(server, claims: TokenClaims) -> None:
    if claims.principal is None or claims.principal.type != "user":
        return
    connection = await mcp_connection_store.get(
        claims.workspace_id, str(server.id), claims.principal.id
    )
    if connection is not None:
        await mcp_connection_store.set_state(
            connection,
            "error",
            error_code="MCP_PAT_RUNTIME_AUTH_REJECTED",
        )
        GATEWAY_MCP_RUNTIME_AUTH_REJECTIONS_TOTAL.labels(
            scope_type=claims.scope.type
        ).inc()


async def personal_connection_headers(
    server, claims: TokenClaims, tool_name: str
) -> dict[str, str]:
    require_remote_mcp_enabled()
    if claims.principal is None or claims.principal.type != "user":
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="user_principal_required"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_PAT_USER_PRINCIPAL_REQUIRED",
                "message": "Personal MCP authentication requires a user principal.",
                "serverId": str(server.id),
            },
        )
    user_id = claims.principal.id
    connection = await mcp_connection_store.get(
        claims.workspace_id, str(server.id), user_id
    )
    if not mcp_connection_store.is_ready(connection):
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="personal_connection_not_ready"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_PERSONAL_CONNECTION_REQUIRED",
                "message": (
                    "Verify this MCP server connection before starting the run."
                    if connection
                    else "Connect this MCP server before starting the run."
                ),
                "serverId": str(server.id),
                "action": (
                    "verify_mcp_server" if connection else "connect_mcp_server"
                ),
            },
        )
    if not mcp_connection_store.has_verified_tool(connection, tool_name):
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="personal_tool_unavailable"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_PERSONAL_TOOL_UNAVAILABLE",
                "message": "This PAT has not verified the requested approved tool.",
                "serverId": str(server.id),
                "toolName": tool_name,
                "action": "verify_mcp_server",
            },
        )
    assert connection is not None
    try:
        credential = await secret_store.get_secret(
            connection.access_secret_name,
            {"workspace_id": claims.workspace_id},
        )
    except SecretNotFoundError as exc:
        await mcp_connection_store.set_state(
            connection, "error", error_code="MCP_PAT_SECRET_MISSING"
        )
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="personal_secret_missing"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_PERSONAL_CONNECTION_REQUIRED",
                "message": "Replace the PAT for this MCP server before starting the run.",
                "serverId": str(server.id),
                "action": "verify_mcp_server",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MCP_SECRET_BACKEND_UNAVAILABLE",
                "message": "MCP credential storage is temporarily unavailable.",
                "serverId": str(server.id),
            },
        ) from exc
    header_name = server.auth_header_name or "Authorization"
    prefix = server.auth_header_prefix
    if prefix is None:
        prefix = "Bearer " if server.auth_type == "bearer_token" else ""
    header_value = f"{prefix}{credential}"
    try:
        validate_auth_header_value(header_value)
    except ValueError as exc:
        await mcp_connection_store.set_state(
            connection, "error", error_code="MCP_PAT_HEADER_INVALID"
        )
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="personal_header_invalid"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_PERSONAL_CONNECTION_REQUIRED",
                "message": "Replace the PAT for this MCP server before starting the run.",
                "serverId": str(server.id),
                "action": "verify_mcp_server",
            },
        ) from exc
    return {header_name: header_value}
