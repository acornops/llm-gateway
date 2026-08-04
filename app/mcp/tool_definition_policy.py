"""Security policy for authenticated shared MCP tool definitions."""

from __future__ import annotations


class McpToolDefinitionConflictError(ValueError):
    """An authenticated peer changed a security-relevant shared tool definition."""


def ensure_discovered_tool_compatible(current, observed) -> None:
    """Reject discovered schema changes to reviewed shared tools.

    ``capability`` is operator-configured policy rather than an immutable
    upstream declaration. A later tools/list response may still declare a
    tool as write-capable after an operator has restricted its effective
    capability to read, so that difference must not block credential
    verification.
    """

    schema_changed = (
        getattr(current, "input_schema", None)
        != getattr(observed, "input_schema", None)
        or getattr(current, "output_schema", None)
        != getattr(observed, "output_schema", None)
    )
    if schema_changed:
        raise McpToolDefinitionConflictError(
            "Authenticated MCP tool definition conflicts with the reviewed definition"
        )
