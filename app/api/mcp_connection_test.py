"""Credential-free MCP connection-test implementation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException

from app.api.mcp_admin_schemas import (
    McpServerConnectionTestResponse,
    ToolConfigRequest,
)
from app.mcp.logging import loggable_mcp_server_origin


async def run_mcp_server_connection_test(
    *,
    server_id: str,
    workspace_id: str,
    target_id: str | None,
    target_type: str | None,
    scope_type: Literal["agent", "target"],
    agent_id: str | None,
    registry_destination: Any,
    registry_scope_options: Any,
    server_registry: Any,
    require_remote_mcp_enabled: Any,
    discover_server_tools: Any,
    merge_connection_discovery: Any,
    record_discovery_status: Any,
    logger: Any,
) -> McpServerConnectionTestResponse:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    registry_scope = registry_scope_options(scope_type, destination_target_type)
    server = await server_registry.get_server(
        workspace_id,
        destination_id,
        server_id,
        **registry_scope,
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if server.credential_mode != "none":
        raise HTTPException(
            status_code=409,
            detail="Use the connection verify endpoint for authenticated discovery",
        )
    if getattr(server, "provenance_type", "manual") != "builtin":
        require_remote_mcp_enabled()

    discovered_tools: list[ToolConfigRequest] = []
    discovery_error: str | None = None
    try:
        discovered_tools, discovery_error, _discovery_error_code = (
            await discover_server_tools(workspace_id, destination_id, server)
        )
        if discovery_error is None:
            await merge_connection_discovery(server, discovered_tools)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        discovery_error = detail or "MCP server discovery failed."
    except Exception:
        logger.exception(
            "mcp_tool_discovery_test_failed",
            workspace_id=workspace_id,
            scope_type=scope_type,
            destination_id=destination_id,
            server_name=server.server_name,
            server_url=loggable_mcp_server_origin(server.server_url),
        )
        discovery_error = "MCP server discovery failed."

    updated_server = await record_discovery_status(
        workspace_id,
        destination_id,
        server_id,
        discovery_error,
        **registry_scope,
    )
    if updated_server is not None:
        server = updated_server
    timestamp = server.last_discovery_at or datetime.now(UTC)
    discovered_tool_names = sorted({tool.name for tool in discovered_tools})
    return McpServerConnectionTestResponse(
        server_id=str(server.id),
        server_name=server.server_name,
        server_url=server.server_url,
        connection_status="error" if discovery_error else "ok",
        last_discovery_at=timestamp,
        discovered_tool_count=len(discovered_tool_names),
        discovered_tools=discovered_tool_names,
        error=discovery_error,
    )
