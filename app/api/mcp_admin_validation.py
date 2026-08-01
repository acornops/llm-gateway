from typing import Any, Literal, TypedDict
from urllib.parse import parse_qsl, urlparse

from fastapi import HTTPException

from app.api.mcp_admin_schemas import McpServerCreateRequest
from app.config.settings import settings

_SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}


class AgentRegistryScope(TypedDict):
    scope_type: Literal["agent"]


class TargetRegistryScope(TypedDict):
    scope_type: Literal["target"]
    target_type: str


RegistryScope = AgentRegistryScope | TargetRegistryScope


def validate_remote_mcp_endpoint_contract(value: str) -> None:
    """Validate the URL form without guessing whether its path speaks MCP."""
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="MCP endpoint has an invalid host or port"
        ) from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="Remote MCP endpoint must be an absolute HTTPS URL",
        )
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="MCP endpoint must not include credentials")
    if parsed.fragment:
        raise HTTPException(status_code=400, detail="MCP endpoint must not include a fragment")
    query_keys = {key.strip().lower() for key, _value in parse_qsl(parsed.query)}
    if query_keys & _SECRET_QUERY_KEYS:
        raise HTTPException(
            status_code=400,
            detail="MCP endpoint credentials must use the authentication fields",
        )


def validate_registry_scope(
    scope_type: str,
    target_id: str | None,
    target_type: str | None,
    agent_id: str | None = None,
) -> None:
    if scope_type == "agent":
        if not agent_id or target_id is not None or target_type is not None:
            raise HTTPException(status_code=422, detail="agent scope requires agent_id")
        return
    if scope_type != "target" or not target_id or not target_type:
        raise HTTPException(
            status_code=422,
            detail="target scope requires target_id and target_type",
        )


def registry_destination(
    scope_type: str,
    target_id: str | None,
    target_type: str | None,
    agent_id: str | None,
) -> tuple[str, str | None]:
    validate_registry_scope(scope_type, target_id, target_type, agent_id)
    if scope_type == "agent":
        assert agent_id is not None
        return agent_id, None
    assert target_id is not None and target_type is not None
    return target_id, target_type


def registry_scope_options(scope_type: str, target_type: str | None = None) -> RegistryScope:
    """Build discriminated registry kwargs without null target fields in Agent calls."""
    if scope_type == "agent":
        return {"scope_type": "agent"}
    if scope_type != "target" or not target_type:
        raise ValueError("target MCP scope requires target_type")
    return {"scope_type": "target", "target_type": target_type}


def registered_server_destination(server: Any) -> tuple[str, RegistryScope]:
    """Resolve a persisted server through its discriminated Agent/target identity."""
    if server.scope_type == "agent":
        if not server.agent_id:
            raise ValueError("Agent-scoped MCP server is missing agent_id")
        return server.agent_id, registry_scope_options("agent")
    if server.scope_type != "target" or not server.target_id or not server.target_type:
        raise ValueError("target-scoped MCP server is missing target identity")
    return server.target_id, registry_scope_options("target", server.target_type)


def registered_server_request_context(
    workspace_id: str, server: Any
) -> tuple[str, RegistryScope, dict[str, str]]:
    destination_id, registry_scope = registered_server_destination(server)
    return (
        destination_id,
        registry_scope,
        registry_request_headers(workspace_id, destination_id, registry_scope),
    )


def registry_request_headers(
    workspace_id: str, destination_id: str, registry_scope: RegistryScope
) -> dict[str, str]:
    headers = {"x-workspace-id": workspace_id}
    if registry_scope["scope_type"] == "agent":
        headers["x-agent-id"] = destination_id
    else:
        headers["x-target-id"] = destination_id
        headers["x-target-type"] = registry_scope["target_type"]
    return headers


def is_builtin_bridge_registration(request: McpServerCreateRequest) -> bool:
    return (
        request.server_url == settings.BUILTIN_TARGET_MCP_SERVER_URL
        and request.auth_type == "none"
        and request.credential_mode == "none"
        and request.auth_header_name is None
        and request.auth_header_prefix is None
        and request.public_headers is None
        and len(request.tools) > 0
        and all(tool.source == "builtin" for tool in request.tools)
    )
