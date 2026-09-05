"""Fail-closed tool reconciliation after MCP request-trust changes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.api.mcp_admin_helpers import (
    _apply_tools_for_server,
    _discover_server_tools,
    _record_discovery_status,
)
from app.mcp.registry.store import mcp_server_registry, tool_registry


async def reconcile_credential_free_trust(
    *,
    workspace_id: str,
    destination_id: str,
    server_id: str,
    server: Any,
    registry_scope: dict[str, str],
) -> Any:
    """Keep a transitioned server fenced until its tools are authoritative."""

    tools, discovery_error, _discovery_error_code = await _discover_server_tools(
        workspace_id,
        destination_id,
        server,
    )
    await _apply_tools_for_server(
        workspace_id,
        destination_id,
        tools,
        server_id=server_id,
        remove_disabled=False,
        **registry_scope,
    )
    if discovery_error is None:
        await tool_registry.remove_server_tools_not_in(
            workspace_id,
            destination_id,
            server_id=server_id,
            tool_names={tool.name for tool in tools},
            **registry_scope,
        )
    status_server = await _record_discovery_status(
        workspace_id,
        destination_id,
        server_id,
        discovery_error,
        **registry_scope,
    )
    server = status_server or server
    if discovery_error is not None:
        raise HTTPException(
            status_code=503,
            detail=(
                "MCP trust update remains fenced because tool discovery failed; "
                "retry this update"
            ),
        )
    reconciled = await mcp_server_registry.update_server(
        workspace_id,
        destination_id,
        server_id,
        {
            "credential_transitioning": False,
            "connection_status": "unknown",
        },
        **registry_scope,
    )
    if not reconciled:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return reconciled
