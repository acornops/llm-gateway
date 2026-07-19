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


def validate_remote_mcp_endpoint_contract(value: str) -> None:
    """Reject non-endpoint and credential-bearing manual MCP locations."""
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
        raise HTTPException(
            status_code=400, detail="MCP endpoint must not include credentials"
        )
    if parsed.fragment:
        raise HTTPException(
            status_code=400, detail="MCP endpoint must not include a fragment"
        )
    query_keys = {key.strip().lower() for key, _value in parse_qsl(parsed.query)}
    if query_keys & _SECRET_QUERY_KEYS:
        raise HTTPException(
            status_code=400,
            detail="MCP endpoint credentials must use the authentication fields",
        )
    hostname = parsed.hostname.lower()
    path = parsed.path.lower().rstrip("/")
    if (
        hostname
        in {"github.com", "gitlab.com", "npmjs.com", "www.npmjs.com", "pypi.org"}
        or path == "/v0.1"
        or path.startswith("/v0.1/")
        or path.endswith(("/server.json", ".git"))
    ):
        raise HTTPException(
            status_code=400,
            detail="Connect by URL accepts only a remote Streamable HTTP MCP endpoint",
        )


def validate_registry_scope(
    scope_type: str, target_id: str, target_type: str, agent_id: str | None = None
) -> None:
    if scope_type == "agent":
        if not agent_id or target_id != agent_id or target_type != "agent":
            raise HTTPException(status_code=422, detail="agent scope requires agent_id")
        return
    if scope_type != "target" or target_type == "agent":
        raise HTTPException(
            status_code=422, detail="target scope requires a concrete target type"
        )


def is_builtin_bridge_registration(request: McpServerCreateRequest) -> bool:
    return (
        request.server_url == settings.BUILTIN_TARGET_MCP_SERVER_URL
        and request.auth_type == "none"
        and request.auth_secret_name is None
        and request.auth_secret_value is None
        and request.auth_header_name is None
        and request.auth_header_prefix is None
        and request.public_headers is None
        and len(request.tools) > 0
        and all(tool.source == "builtin" for tool in request.tools)
    )
