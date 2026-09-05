"""Canonical identities shared by MCP persistence, locks, and secret stores."""

from __future__ import annotations

from uuid import UUID


def canonical_mcp_server_id(server_id: object) -> str:
    """Collapse every valid UUID spelling onto its canonical lowercase form.

    Test doubles and legacy callers sometimes use non-UUID identifiers. Those
    cannot resolve a production UUID row, but preserving them keeps the helper
    deterministic while all valid aliases share one lock and secret identity.
    """

    value = str(server_id)
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        return value
