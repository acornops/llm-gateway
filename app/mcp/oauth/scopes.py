"""Validation for untrusted OAuth scope values."""

from __future__ import annotations

from collections.abc import Iterable

from app.mcp.oauth.errors import oauth_error

MAX_OAUTH_SCOPES = 100
MAX_OAUTH_SCOPE_LENGTH = 256
MAX_OAUTH_SCOPE_BYTES = 4096


def normalize_oauth_scopes(
    values: Iterable[object],
    *,
    error_code: str,
    error_message: str,
) -> list[str]:
    """Return unique RFC 6749 scope tokens with explicit storage bounds."""

    scopes: set[str] = set()
    total_bytes = 0
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_OAUTH_SCOPE_LENGTH
            or any(
                not (
                    ord(character) == 0x21
                    or 0x23 <= ord(character) <= 0x5B
                    or 0x5D <= ord(character) <= 0x7E
                )
                for character in value
            )
        ):
            raise oauth_error(error_code, error_message)
        if value in scopes:
            continue
        scopes.add(value)
        total_bytes += len(value.encode())
        if len(scopes) > MAX_OAUTH_SCOPES or total_bytes > MAX_OAUTH_SCOPE_BYTES:
            raise oauth_error(error_code, error_message)
    return sorted(scopes)
