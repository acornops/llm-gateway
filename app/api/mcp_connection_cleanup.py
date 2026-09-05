"""Retry-safe terminal cleanup for MCP connection secrets and rows."""

from __future__ import annotations

from contextlib import suppress

import structlog

from app.mcp.connections import (
    ConnectionOwner,
    credential_secret_name,
    mcp_connection_store,
)
from app.mcp.oauth.flow_store import oauth_flow_store
from app.mcp.oauth.registration_store import oauth_registration_store
from app.mcp.oauth.tokens import oauth_token_service
from app.observability.metrics import GATEWAY_MCP_SECRET_CLEANUP_TOTAL
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store

logger = structlog.get_logger()


async def _delete_owner_secrets(
    workspace_id: str,
    server_id: str,
    owner: ConnectionOwner,
    *,
    reason: str,
) -> None:
    """Delete both deterministic credential identities before removing the row."""

    try:
        with suppress(SecretNotFoundError):
            await secret_store.delete_secret(
                credential_secret_name(workspace_id, server_id, owner),
                {"workspace_id": workspace_id},
            )
        await oauth_token_service.delete_tokens(workspace_id, server_id, owner.owner_id)
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="success").inc()
    except Exception:
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="error").inc()
        logger.exception(
            "mcp_credential_cleanup_failed",
            workspace_id=workspace_id,
            server_id=server_id,
            owner_type=owner.owner_type,
            reason=reason,
        )
        raise


async def cleanup_connection_state(
    workspace_id: str,
    server_id: str,
    owner: ConnectionOwner,
    connection,
    *,
    reason: str,
) -> None:
    """Remove both credential identities, retaining the row if cleanup fails.

    Historical or partial auth transitions can leave the opposite secret type
    behind, including when the connection row is already absent. Callers hold
    the relevant mutation locks throughout this cleanup.
    """

    if connection is not None and getattr(connection, "oauth_issuer", None):
        await oauth_token_service.revoke(
            workspace_id=workspace_id,
            server_id=server_id,
            owner_id=owner.owner_id,
            connection=connection,
        )
    await oauth_flow_store.delete_for_connection(
        workspace_id,
        server_id,
        owner.owner_id,
    )
    await _delete_owner_secrets(
        workspace_id,
        server_id,
        owner,
        reason=reason,
    )
    if connection is not None:
        await mcp_connection_store.delete(workspace_id, server_id, owner)


async def cleanup_server_connections(
    workspace_id: str, server_id: str, *, reason: str = "installation_delete"
) -> int:
    connections = await mcp_connection_store.list_for_server(workspace_id, server_id)
    for connection in connections:
        owner = ConnectionOwner(connection.owner_type, connection.owner_id)
        async with mcp_connection_store.mutation_lock(workspace_id, server_id, owner):
            current = await mcp_connection_store.get(workspace_id, server_id, owner)
            await cleanup_connection_state(
                workspace_id,
                server_id,
                owner,
                current,
                reason=reason,
            )
    await oauth_flow_store.delete_for_server(workspace_id, server_id)
    await oauth_registration_store.delete_for_server(workspace_id, server_id)
    await secret_store.purge_mcp_secrets(workspace_id, server_id=server_id)
    if await secret_store.count_mcp_secrets(workspace_id, server_id=server_id):
        raise RuntimeError("MCP server cleanup left secret objects")
    return len(connections)


async def cleanup_user_server_connection(
    workspace_id: str,
    server_id: str,
    user_id: str,
    *,
    reason: str = "member_removal",
) -> bool:
    """Delete all credential state for one user while a server lock is held."""

    owner = ConnectionOwner("user", user_id)
    async with mcp_connection_store.mutation_lock(workspace_id, server_id, owner):
        current = await mcp_connection_store.get(workspace_id, server_id, owner)
        if current is None:
            # Lifecycle staging has already fenced new owner work. The caller
            # waits every server lock, then performs one workspace/owner secret
            # inventory and flow purge, which covers rowless and UUID-alias
            # state without two point deletes per absent server.
            return False
        await cleanup_connection_state(
            workspace_id,
            server_id,
            owner,
            current,
            reason=reason,
        )
        return True
