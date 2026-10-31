import json
import re
from typing import Any

DISCOVERED_TOOL_DESCRIPTION_MAX_CHARS = 500
DISCOVERED_TOOL_SCHEMA_MAX_BYTES = 8192
DISCOVERED_TOOL_SCHEMA_MAX_DEPTH = 8
DISCOVERED_TOOL_SCHEMA_MAX_ITEMS = 100
PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|messages|rules)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:reveal|print|dump|exfiltrate)\b.*\b"
        r"(?:system prompt|developer message|secret|api key|token)\b",
        re.I,
    ),
    re.compile(r"\b(?:bypass|disable)\s+(?:safety|policy|guardrails|rules)\b", re.I),
    re.compile(r"\bjailbreak\b", re.I),
)


def _contains_prompt_injection_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in PROMPT_INJECTION_PATTERNS)


def sanitize_discovered_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    if not normalized or _contains_prompt_injection_text(normalized):
        return None
    return normalized[:DISCOVERED_TOOL_DESCRIPTION_MAX_CHARS]


_SCHEMA_MAP_KEYS = {
    "properties",
    "patternProperties",
    "definitions",
    "$defs",
    "dependentSchemas",
    "dependencies",
}
_SCHEMA_CHILD_KEYS = {
    "additionalProperties",
    "additionalItems",
    "items",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
}


class ToolSchemaRejectedError(ValueError):
    """A schema cannot be preserved within the metadata boundary."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"TOOL_SCHEMA_REJECTED: {reason}")


def _prepare_schema(
    schema: Any, *, max_depth: int, max_bytes: int, max_items: int, sanitize_text: Any
) -> dict[str, Any]:
    if type(schema) is not dict:
        raise ToolSchemaRejectedError("expected_object")
    size = 0
    visited = 0
    ancestors: set[int] = set()

    def consume(amount: int) -> None:
        nonlocal size
        size += amount
        if size > max_bytes:
            raise ToolSchemaRejectedError("byte_limit")

    def scalar_size(value: Any) -> int:
        if isinstance(value, str) and len(value) > max_bytes:
            raise ToolSchemaRejectedError("byte_limit")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            return len(encoded.encode("utf-8", errors="backslashreplace"))
        except (TypeError, ValueError):
            raise ToolSchemaRejectedError("non_json_value") from None

    def visit(value: Any, depth: int, context: str) -> Any:
        nonlocal visited
        visited += 1
        if depth > max_depth:
            raise ToolSchemaRejectedError("depth_limit")
        if visited > 16_384:
            raise ToolSchemaRejectedError("value_limit")
        if type(value) in (dict, list):
            if id(value) in ancestors:
                raise ToolSchemaRejectedError("cycle")
            if len(value) > max_items:
                raise ToolSchemaRejectedError("container_limit")
            consume(2 + max(0, len(value) - 1))
            ancestors.add(id(value))
            try:
                if isinstance(value, list):
                    child_context = "schema" if context == "schema" else "literal"
                    return [visit(item, depth + 1, child_context) for item in value]
                result = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ToolSchemaRejectedError("non_string_key")
                    consume(scalar_size(key) + 1)
                    child_context = "literal"
                    if context == "schema_map":
                        child_context = "schema" if isinstance(item, dict) else "literal"
                    elif context == "schema":
                        if key in _SCHEMA_MAP_KEYS:
                            child_context = "schema_map"
                        elif key in _SCHEMA_CHILD_KEYS:
                            child_context = "schema"
                    prepared = visit(item, depth + 1, child_context)
                    if (
                        context == "schema"
                        and isinstance(item, str)
                        and key in {"description", "markdownDescription", "title"}
                    ):
                        prepared = sanitize_text(item)
                        if prepared is None:
                            continue
                    result[key] = prepared
                return result
            finally:
                ancestors.remove(id(value))
        if value is not None and type(value) not in (str, bool, int, float):
            raise ToolSchemaRejectedError("non_json_value")
        consume(scalar_size(value))
        return value

    return visit(schema, 0, "schema")


def sanitize_discovered_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    return _prepare_schema(
        schema,
        max_depth=DISCOVERED_TOOL_SCHEMA_MAX_DEPTH,
        max_bytes=DISCOVERED_TOOL_SCHEMA_MAX_BYTES,
        max_items=DISCOVERED_TOOL_SCHEMA_MAX_ITEMS,
        sanitize_text=sanitize_discovered_text,
    )


def extract_discovery_error(payload: dict[str, Any]) -> str | None:
    if not payload.get("isError"):
        return None

    content = payload.get("content")
    if isinstance(content, list):
        messages: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
        if messages:
            return " | ".join(messages)

    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "MCP server tool discovery failed."
