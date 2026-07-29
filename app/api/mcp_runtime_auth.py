import hashlib
import secrets
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
from app.mcp.oauth.errors import McpOAuthError
from app.mcp.oauth.scopes import normalize_oauth_scopes
from app.mcp.oauth.tokens import oauth_token_service
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.observability.metrics import (
    GATEWAY_MCP_READINESS_FAILURES_TOTAL,
    GATEWAY_MCP_RUNTIME_AUTH_REJECTIONS_TOTAL,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store


class McpRequestHeaders(dict[str, str]):
    """Headers plus local-only identity for stale authentication-failure checks."""

    def __init__(
        self,
        values: Mapping[str, str],
        *,
        connection_id: str | None = None,
        credential_fingerprint: str | None = None,
    ) -> None:
        super().__init__(values)
        self.connection_id = connection_id
        self.credential_fingerprint = credential_fingerprint


def _credential_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _principal(claims: TokenClaims) -> tuple[str | None, str | None]:
    if claims.principal is None:
        return None, None
    return claims.principal.type, claims.principal.id


async def mark_connection_error(
    server,
    claims: TokenClaims,
    *,
    auth_error: str | None = None,
    required_scopes: list[str] | None = None,
    expected_connection_id: str | None = None,
    expected_credential_fingerprint: str | None = None,
) -> None:
    principal_type, principal_id = _principal(claims)
    try:
        owner = resolve_connection_owner(server, principal_type, principal_id)
    except ConnectionOwnerError:
        return
    if owner is None:
        return
    async with mcp_connection_store.mutation_lock(
        claims.workspace_id,
        str(server.id),
        owner,
    ):
        connection = await mcp_connection_store.get(
            claims.workspace_id,
            str(server.id),
            owner,
        )
        if connection is None:
            return
        if (
            expected_connection_id is not None
            and str(connection.id) != expected_connection_id
        ):
            return
        oauth = getattr(server, "auth_type", "none") == "oauth"
        if expected_credential_fingerprint is not None:
            if oauth:
                matches = await oauth_token_service.access_token_matches(
                    claims.workspace_id,
                    str(server.id),
                    owner.owner_id,
                    expected_credential_fingerprint,
                )
            else:
                try:
                    current_credential = await secret_store.get_secret(
                        credential_secret_name(
                            claims.workspace_id,
                            str(server.id),
                            owner,
                        ),
                        {"workspace_id": claims.workspace_id},
                    )
                except Exception:
                    return
                matches = secrets.compare_digest(
                    _credential_fingerprint(current_credential),
                    expected_credential_fingerprint,
                )
            if not matches:
                return
        insufficient_scope = oauth and auth_error == "insufficient_scope"
        current_scopes = list(getattr(connection, "oauth_scopes", []) or [])
        try:
            scopes = normalize_oauth_scopes(
                [*current_scopes, *(required_scopes or [])],
                error_code="MCP_OAUTH_RUNTIME_SCOPE_INVALID",
                error_message="The MCP server returned invalid required scopes.",
            )
        except McpOAuthError:
            scopes = current_scopes
        await mcp_connection_store.set_state(
            connection,
            (
                "reauthorization_required"
                if oauth
                else "error"
            ),
            error_code=(
                "MCP_OAUTH_INSUFFICIENT_SCOPE"
                if insufficient_scope
                else "MCP_OAUTH_RUNTIME_AUTH_REJECTED"
                if oauth
                else "MCP_CREDENTIAL_RUNTIME_AUTH_REJECTED"
            ),
            oauth_scopes=scopes if oauth else None,
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
        return McpRequestHeaders(
            build_mcp_request_headers(server, None, platform_headers=platform_headers)
        )
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
                    "Authorize this MCP server before starting the run."
                    if getattr(server, "auth_type", "none") == "oauth"
                    else "Verify this MCP server connection before starting the run."
                    if connection
                    else "Connect this MCP server before starting the run."
                ),
                "serverId": str(server.id),
                "action": (
                    "reauthorize_mcp_server"
                    if getattr(connection, "status", None) == "reauthorization_required"
                    else "authorize_mcp_server"
                    if getattr(server, "auth_type", "none") == "oauth"
                    else "verify_mcp_server"
                    if connection
                    else "connect_mcp_server"
                ),
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
    if getattr(server, "auth_type", "none") == "oauth":
        try:
            credential = await oauth_token_service.access_token(
                workspace_id=claims.workspace_id,
                server_id=str(server.id),
                owner_id=owner.owner_id,
                connection=connection,
            )
        except McpOAuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "serverId": str(server.id),
                    "action": (
                        "reauthorize_mcp_server"
                        if exc.code == "MCP_OAUTH_REAUTHORIZATION_REQUIRED"
                        else None
                    ),
                },
            ) from exc
        return McpRequestHeaders(
            build_mcp_request_headers(
                server,
                credential,
                platform_headers=platform_headers,
            ),
            connection_id=str(connection.id),
            credential_fingerprint=_credential_fingerprint(credential),
        )
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
        return McpRequestHeaders(
            build_mcp_request_headers(
                server,
                credential,
                platform_headers=platform_headers,
            ),
            connection_id=str(connection.id),
            credential_fingerprint=_credential_fingerprint(credential),
        )
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
