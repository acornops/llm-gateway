import re
from collections.abc import Mapping
from typing import Any

MAX_PUBLIC_HEADERS = 64
MAX_HEADER_NAME_LENGTH = 128
MAX_HEADER_VALUE_LENGTH = 4096

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

_PUBLIC_HEADER_DENYLIST = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
}

_PUBLIC_HEADER_DENY_PATTERNS = (
    "token",
    "secret",
    "credential",
    "api-key",
    "apikey",
)

MCP_TRANSPORT_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-encoding",
        "content-type",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
    }
)

_RESERVED_HEADER_NAMES = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "proxy-connection",
    "upgrade",
    "keep-alive",
    "te",
    "trailer",
    "x-workspace-id",
    "x-target-id",
    "x-target-type",
    "x-run-id",
    "x-tool-name",
    *MCP_TRANSPORT_HEADER_NAMES,
}


def _validate_header_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("header names must be strings")
    if name != name.strip() or not name:
        raise ValueError("header names must not be empty or padded")
    if len(name) > MAX_HEADER_NAME_LENGTH:
        raise ValueError(f"header names must be {MAX_HEADER_NAME_LENGTH} characters or fewer")
    if not _HEADER_NAME_RE.match(name):
        raise ValueError("header names must be valid HTTP header tokens")
    return name.lower()


def _validate_header_value(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("header values must be strings")
    if len(value) > MAX_HEADER_VALUE_LENGTH:
        raise ValueError(
            f"header values must be {MAX_HEADER_VALUE_LENGTH} characters or fewer"
        )
    if "\r" in value or "\n" in value:
        raise ValueError("header values must not contain CR or LF characters")


def validate_auth_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    _validate_header_value(value)
    return value


def validate_public_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if headers is None:
        return None
    if len(headers) > MAX_PUBLIC_HEADERS:
        raise ValueError(f"public_headers may include at most {MAX_PUBLIC_HEADERS} headers")

    seen: set[str] = set()
    for name, value in headers.items():
        normalized = _validate_header_name(name)
        if normalized in seen:
            raise ValueError(f"duplicate public header name: {name}")
        seen.add(normalized)
        if normalized in _RESERVED_HEADER_NAMES:
            raise ValueError(f"public header {name} is reserved by the platform")
        if normalized in _PUBLIC_HEADER_DENYLIST or any(
            pattern in normalized for pattern in _PUBLIC_HEADER_DENY_PATTERNS
        ):
            raise ValueError(f"public header {name} may not contain credentials")
        _validate_header_value(value)

    return headers


def validate_auth_header_name(name: str | None) -> str | None:
    if name is None:
        return None
    normalized = _validate_header_name(name)
    if normalized in _RESERVED_HEADER_NAMES:
        raise ValueError(f"auth header {name} is reserved by the platform")
    return name


def build_mcp_request_headers(
    server: Any,
    credential: str | None,
    *,
    platform_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build public, platform, and credential headers in one stable order.

    Public installation headers are applied first, platform routing headers
    second, and the authentication header last. Callers must never log the
    returned mapping because it can contain plaintext credentials.
    """
    headers = validate_public_headers(dict(server.public_headers or {})) or {}
    headers.update(dict(platform_headers or {}))
    auth_type = getattr(server, "auth_type", "none")
    if auth_type == "none":
        if credential is not None:
            raise ValueError("unauthenticated MCP installations do not accept credentials")
        return headers
    if credential is None:
        raise ValueError("authenticated MCP installations require a credential")
    validate_auth_header_value(credential)
    if auth_type == "bearer_token":
        header_name = "Authorization"
        prefix = "Bearer "
    elif auth_type == "custom_header":
        header_name = validate_auth_header_name(server.auth_header_name)
        if not header_name:
            raise ValueError("custom-header MCP authentication requires a header name")
        prefix = server.auth_header_prefix or ""
    else:
        raise ValueError("unsupported MCP authentication type")
    header_value = f"{prefix}{credential}"
    validate_auth_header_value(header_value)
    headers[header_name] = header_value
    return headers
