import re

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
