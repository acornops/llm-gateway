from collections.abc import Mapping

from fastapi import HTTPException

from app.auth.claims import TokenClaims
from app.mcp.connections import (
    ConnectionOwnerError,
    credential_secret_name,
    mcp_connection_store,
    resolve_connection_owner,
)
from app.mcp.header_policy import build_mcp_request_headers
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.observability.metrics import (
    GATEWAY_MCP_READINESS_FAILURES_TOTAL,
    GATEWAY_MCP_RUNTIME_AUTH_REJECTIONS_TOTAL,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store


def _principal(claims: TokenClaims) -> tuple[str | None, str | None]:
    if claims.principal is None:
        return None, None
    return claims.principal.type, claims.principal.id


async def mark_connection_error(server, claims: TokenClaims) -> None:
    principal_type, principal_id = _principal(claims)
    try:
        owner = resolve_connection_owner(server, principal_type, principal_id)
    except ConnectionOwnerError:
        return
    if owner is None:
        return
    connection = await mcp_connection_store.get(claims.workspace_id, str(server.id), owner)
    if connection is not None:
        await mcp_connection_store.set_state(
            connection,
            "error",
            error_code="MCP_CREDENTIAL_RUNTIME_AUTH_REJECTED",
        )
        GATEWAY_MCP_RUNTIME_AUTH_REJECTIONS_TOTAL.labels(scope_type=claims.scope.type).inc()


async def connection_request_headers(
    server,
    claims: TokenClaims,
    tool_name: str,
    *,
    platform_headers: Mapping[str, str],
) -> dict[str, str]:
    """Resolve one owner and build the complete runtime request header set."""
    require_remote_mcp_enabled()
    if getattr(server, "credential_transitioning", False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_INSTALLATION_UNAVAILABLE",
                "message": "Credential ownership is being updated. Retry later.",
                "serverId": str(server.id),
            },
        )
    principal_type, principal_id = _principal(claims)
    try:
        owner = resolve_connection_owner(server, principal_type, principal_id)
    except ConnectionOwnerError as exc:
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="individual_user_principal_required"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_INDIVIDUAL_USER_PRINCIPAL_REQUIRED",
                "message": "Individual MCP credentials require a user principal.",
                "serverId": str(server.id),
            },
        ) from exc
    if owner is None:
        return build_mcp_request_headers(server, None, platform_headers=platform_headers)
    connection = await mcp_connection_store.get(claims.workspace_id, str(server.id), owner)
    if not mcp_connection_store.is_ready(connection):
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="connection_not_ready"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CONNECTION_REQUIRED",
                "message": (
                    "Verify this MCP server connection before starting the run."
                    if connection
                    else "Connect this MCP server before starting the run."
                ),
                "serverId": str(server.id),
                "action": "verify_mcp_server" if connection else "connect_mcp_server",
            },
        )
    if not mcp_connection_store.has_verified_tool(connection, tool_name):
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="credential_tool_unavailable"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CREDENTIAL_TOOL_UNAVAILABLE",
                "message": "The connected credential has not verified the requested tool.",
                "serverId": str(server.id),
                "toolName": tool_name,
                "action": "verify_mcp_server",
            },
        )
    assert connection is not None
    secret_name = credential_secret_name(claims.workspace_id, str(server.id), owner)
    try:
        credential = await secret_store.get_secret(
            secret_name, {"workspace_id": claims.workspace_id}
        )
    except SecretNotFoundError as exc:
        await mcp_connection_store.set_state(
            connection, "error", error_code="MCP_CREDENTIAL_SECRET_MISSING"
        )
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="credential_secret_missing"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CONNECTION_REQUIRED",
                "message": "Replace the credential for this MCP server before starting the run.",
                "serverId": str(server.id),
                "action": "connect_mcp_server",
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
    try:
        return build_mcp_request_headers(server, credential, platform_headers=platform_headers)
    except ValueError as exc:
        await mcp_connection_store.set_state(
            connection, "error", error_code="MCP_CREDENTIAL_HEADER_INVALID"
        )
        GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
            scope_type=claims.scope.type, reason="credential_header_invalid"
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CONNECTION_REQUIRED",
                "message": "Replace the credential for this MCP server before starting the run.",
                "serverId": str(server.id),
                "action": "connect_mcp_server",
            },
        ) from exc
