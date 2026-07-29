"""Secret-free MCP connection response mapping."""

from __future__ import annotations

from urllib.parse import urlparse

from app.api.mcp_admin_schemas import McpConnectionResponse


def connection_response(server, connection=None) -> McpConnectionResponse:
    status = connection.status if connection is not None else "missing"
    oauth = server.auth_type == "oauth"
    issuer = getattr(connection, "oauth_issuer", None) if connection is not None else None
    issuer_origin = None
    if issuer:
        parsed = urlparse(issuer)
        issuer_origin = f"{parsed.scheme}://{parsed.netloc}"
    if oauth:
        if status == "reauthorization_required":
            action = "reauthorize_mcp_server"
        elif status in {"missing", "pending_authorization"}:
            action = "authorize_mcp_server"
        elif status == "error":
            action = "verify_mcp_server"
        else:
            action = None
    else:
        action = (
            "connect_mcp_server"
            if status == "missing"
            else "verify_mcp_server"
            if status == "error"
            else None
        )
    return McpConnectionResponse(
        server_id=str(server.id),
        credential_mode=server.credential_mode,
        status=status,
        auth_type=server.auth_type,
        action=action,
        error_code=connection.error_code if connection is not None else None,
        issuer_origin=issuer_origin,
        registration_method=(
            getattr(connection, "oauth_registration_method", None)
            if connection is not None
            else None
        ),
        scopes=list(getattr(connection, "oauth_scopes", []) or []),
        token_expires_at=(
            getattr(connection, "oauth_token_expires_at", None)
            if connection is not None
            else None
        ),
        refresh_capable=bool(
            getattr(connection, "oauth_refresh_capable", False)
            if connection is not None
            else False
        ),
        verified_at=getattr(connection, "verified_at", None),
        updated_at=getattr(connection, "updated_at", None),
    )
