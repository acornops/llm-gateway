"""Security policy for authenticated shared MCP tool definitions."""

from __future__ import annotations


class McpToolDefinitionConflictError(ValueError):
    """An authenticated peer changed a security-relevant shared tool definition."""


def ensure_discovered_tool_compatible(current, observed) -> None:
    """Prevent one credential owner from widening shared reviewed authority."""

    schema_changed = (
        getattr(current, "input_schema", None)
        != getattr(observed, "input_schema", None)
        or getattr(current, "output_schema", None)
        != getattr(observed, "output_schema", None)
    )
    authority_increased = (
        getattr(current, "capability", "write") == "read"
        and getattr(observed, "capability", "write") != "read"
    )
    if schema_changed or authority_increased:
        raise McpToolDefinitionConflictError(
            "Authenticated MCP tool definition conflicts with the reviewed definition"
        )
