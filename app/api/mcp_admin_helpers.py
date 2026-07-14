from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException

from app.api.mcp_admin_schemas import (
    McpServerResponse,
    ToolConfigRequest,
    ToolConfigResponse,
)
from app.config.settings import settings
from app.internal_model_tools import is_reserved_internal_tool_name
from app.mcp.header_policy import validate_auth_header_value
from app.mcp.registry.models import McpServer, Tool
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.tool_metadata import (
    extract_discovery_error as _extract_discovery_error,
)
from app.mcp.tool_metadata import (
    sanitize_discovered_schema,
    sanitize_discovered_text,
)
from app.mcp.transports.http_transport import mcp_transport
from app.secrets.store import secret_store

logger = structlog.get_logger()


def _build_tool_response(tool: Tool) -> ToolConfigResponse:
    capability = tool.capability if tool.capability in ("read", "write") else "write"
    source = tool.source if tool.source in ("mcp", "builtin") else "mcp"
    return ToolConfigResponse(
        name=tool.tool_name,
        mcp_server_url=tool.mcp_server_url,
        timeout_ms=tool.timeout_ms,
        description=tool.description,
        capability=capability,
        version=tool.version or "v1",
        source=source,
        input_schema=tool.input_schema,
        output_schema=getattr(tool, "output_schema", None),
        artifact_policy=getattr(tool, "artifact_policy", "never"),
        enabled=bool(tool.enabled),
    )


def _build_server_response(server: McpServer, tools: list[Tool]) -> McpServerResponse:
    return McpServerResponse(
        id=str(server.id),
        workspace_id=server.workspace_id,
        scope_type=getattr(server, "scope_type", "target"),
        target_id=server.target_id,
        target_type=server.target_type,
        server_name=server.server_name,
        server_url=server.server_url,
        enabled=bool(server.enabled),
        auth_type=server.auth_type,
        credential_configured=bool(server.auth_secret_name),
        auth_header_name=server.auth_header_name,
        auth_header_prefix=server.auth_header_prefix,
        public_headers=server.public_headers,
        connection_status=server.connection_status
        if server.connection_status in ("unknown", "ok", "error")
        else "unknown",
        last_discovery_at=server.last_discovery_at,
        last_discovery_error=server.last_discovery_error,
        tools=[_build_tool_response(tool) for tool in tools],
    )


async def _resolve_tools_for_server(
    workspace_id: str,
    target_id: str,
    server_url: str,
    target_type: str,
) -> list[Tool]:
    tools = await tool_registry.list_target_tools(
        workspace_id,
        target_id,
        target_type=target_type,
        include_disabled=True,
    )
    return [tool for tool in tools if tool.mcp_server_url == server_url]


async def _persist_server_secret(
    workspace_id: str,
    target_id: str,
    target_type: str,
    server_name: str,
    secret_name: str | None,
    secret_value: str,
) -> str:
    resolved_name = (
        secret_name
        or f"mcp_server::{workspace_id}::{target_type}::{target_id}::{server_name}"
    )
    await secret_store.put_secret(
        resolved_name,
        secret_value,
        {
            "workspace_id": workspace_id,
            "target_id": target_id,
            "target_type": target_type,
        },
    )
    return resolved_name


def _auth_header_name_for(auth_type: str, header_name: str | None) -> str | None:
    if auth_type == "none":
        return None
    if auth_type == "bearer_token":
        return "Authorization"
    return header_name


def _auth_header_prefix_for(auth_type: str, header_prefix: str | None) -> str | None:
    if auth_type == "none":
        return None
    if auth_type == "bearer_token":
        return "Bearer "
    return header_prefix


async def _build_server_request_headers(
    workspace_id: str, target_id: str, server: McpServer
) -> dict[str, str]:
    headers: dict[str, str] = dict(server.public_headers or {})
    headers.update({
        "x-workspace-id": workspace_id,
        "x-target-id": target_id,
        "x-target-type": server.target_type,
    })
    if server.auth_type in ("bearer_token", "custom_header"):
        if not server.auth_secret_name:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Server auth misconfigured for {server.server_name}:"
                    " missing auth_secret_name"
                ),
            )
        secret_value = await secret_store.get_secret(
            server.auth_secret_name,
            {
                "workspace_id": workspace_id,
                "target_id": target_id,
                "target_type": server.target_type,
            },
        )
        header_name = server.auth_header_name or "Authorization"
        prefix = server.auth_header_prefix or ""
        header_value = f"{prefix}{secret_value}"
        try:
            validate_auth_header_value(header_value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Server auth misconfigured for {server.server_name}: {exc}",
            ) from exc
        headers[header_name] = header_value
    return headers


def _normalize_discovered_tools(payload: dict[str, Any]) -> list[ToolConfigRequest]:
    if payload.get("isError"):
        return []

    tools_payload: list[Any] = []
    if isinstance(payload.get("tools"), list):
        tools_payload = payload["tools"]
    else:
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools_payload = result["tools"]

    discovered: list[ToolConfigRequest] = []
    seen_names: set[str] = set()
    for raw_tool in tools_payload:
        if not isinstance(raw_tool, dict):
            continue
        raw_name = raw_tool.get("name")
        if not isinstance(raw_name, str):
            continue
        tool_name = raw_name.strip()
        if not tool_name or tool_name in seen_names:
            continue
        if is_reserved_internal_tool_name(tool_name):
            logger.warning(
                "mcp_tool_discovery_reserved_tool_skipped",
                tool_name=tool_name,
            )
            continue
        seen_names.add(tool_name)

        description = sanitize_discovered_text(raw_tool.get("description"))

        input_schema = None
        for key in ("input_schema", "inputSchema", "parameters", "json_schema", "schema"):
            candidate = raw_tool.get(key)
            if isinstance(candidate, dict):
                input_schema = sanitize_discovered_schema(candidate)
                break

        output_schema = None
        for key in ("output_schema", "outputSchema"):
            candidate = raw_tool.get(key)
            if isinstance(candidate, dict):
                output_schema = sanitize_discovered_schema(candidate)
                break

        artifact_policy = raw_tool.get("artifactPolicy", "never")
        if artifact_policy not in ("never", "if_detailed", "always"):
            artifact_policy = "never"

        version = raw_tool.get("version")
        if not isinstance(version, str) or not version.strip():
            version = "v1"

        discovered.append(
            ToolConfigRequest(
                name=tool_name,
                timeout_ms=settings.MCP_CALL_DEFAULT_TIMEOUT_MS,
                description=description,
                capability="write",
                version=version,
                source="mcp",
                input_schema=input_schema,
                output_schema=output_schema,
                artifact_policy=artifact_policy,
                enabled=False,
            )
        )
    return discovered


async def _discover_server_tools(
    workspace_id: str, target_id: str, server: McpServer
) -> tuple[list[ToolConfigRequest], str | None]:
    headers = await _build_server_request_headers(workspace_id, target_id, server)
    discovery_response = await mcp_transport.list_tools(
        server.server_url, settings.MCP_CALL_DEFAULT_TIMEOUT_MS, headers
    )
    if not isinstance(discovery_response, dict):
        logger.warning(
            "mcp_tool_discovery_invalid_response",
            workspace_id=workspace_id,
            target_id=target_id,
            server_name=server.server_name,
            server_url=server.server_url,
        )
        return [], "Invalid response payload from MCP server tools/list."

    discovery_error = _extract_discovery_error(discovery_response)
    if discovery_error:
        logger.warning(
            "mcp_tool_discovery_error",
            workspace_id=workspace_id,
            target_id=target_id,
            server_name=server.server_name,
            server_url=server.server_url,
            discovery_error=discovery_error,
        )
        return [], discovery_error

    discovered = _normalize_discovered_tools(discovery_response)
    if len(discovered) == 0:
        logger.warning(
            "mcp_tool_discovery_empty",
            workspace_id=workspace_id,
            target_id=target_id,
            server_name=server.server_name,
            server_url=server.server_url,
        )
    return discovered, None


async def _record_discovery_status(
    workspace_id: str,
    target_id: str,
    server_id: str,
    discovery_error: str | None,
    target_type: str,
) -> McpServer | None:
    return await mcp_server_registry.update_server(
        workspace_id,
        target_id,
        server_id,
        {
            "connection_status": "error" if discovery_error else "ok",
            "last_discovery_at": datetime.now(UTC),
            "last_discovery_error": discovery_error,
        },
        target_type=target_type,
    )


async def _apply_tools_for_server(
    workspace_id: str,
    target_id: str,
    server_url: str,
    tools: list[ToolConfigRequest],
    *,
    target_type: str,
    remove_disabled: bool = True,
) -> None:
    for tool in tools:
        if not tool.enabled and remove_disabled:
            await tool_registry.remove_tool_for_target(
                tool.name,
                workspace_id,
                target_id,
                target_type=target_type,
            )
            continue
        provided_fields = getattr(tool, "model_fields_set", {"capability"})
        capability_provided = "capability" in provided_fields
        existing_tool = None
        if (
            not capability_provided
            and tool.source == "mcp"
        ):
            existing_tool = await tool_registry.get_tool(
                workspace_id,
                target_id,
                tool.name,
                target_type=target_type,
                include_disabled=True,
            )
            if (
                tool.enabled
                and existing_tool is not None
                and existing_tool.source == "mcp"
                and not existing_tool.enabled
            ):
                raise HTTPException(
                    status_code=400,
                    detail="capability is required when enabling a discovered MCP tool",
                )
        capability = tool.capability
        if not capability_provided and existing_tool is not None:
            capability = (
                existing_tool.capability
                if existing_tool.capability in ("read", "write")
                else "write"
            )
        try:
            await tool_registry.upsert_tool(
                tool_name=tool.name,
                mcp_server_url=server_url,
                workspace_id=workspace_id,
                target_id=target_id,
                target_type=target_type,
                timeout_ms=tool.timeout_ms,
                input_schema=tool.input_schema,
                output_schema=getattr(tool, "output_schema", None),
                artifact_policy=getattr(tool, "artifact_policy", "never"),
                enabled=bool(tool.enabled),
                description=tool.description,
                capability=capability,
                version=tool.version,
                source=tool.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
