import json
import re
from typing import Any

DISCOVERED_TOOL_DESCRIPTION_MAX_CHARS = 500
DISCOVERED_TOOL_SCHEMA_MAX_BYTES = 8192
DISCOVERED_TOOL_SCHEMA_MAX_DEPTH = 8
DISCOVERED_TOOL_SCHEMA_MAX_ITEMS = 100
SCHEMA_TEXT_KEYS = {"description", "markdownDescription", "title"}
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


def _sanitize_discovered_schema_value(
    value: Any, *, key: str | None = None, depth: int = 0
) -> Any:
    if depth > DISCOVERED_TOOL_SCHEMA_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in list(value.items())[:DISCOVERED_TOOL_SCHEMA_MAX_ITEMS]:
            if not isinstance(item_key, str):
                continue
            sanitized_value = _sanitize_discovered_schema_value(
                item_value, key=item_key, depth=depth + 1
            )
            if sanitized_value is None and item_key in SCHEMA_TEXT_KEYS:
                continue
            sanitized[item_key] = sanitized_value
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_discovered_schema_value(item, depth=depth + 1)
            for item in value[:DISCOVERED_TOOL_SCHEMA_MAX_ITEMS]
        ]
    if isinstance(value, str):
        if key in SCHEMA_TEXT_KEYS:
            return sanitize_discovered_text(value)
        if _contains_prompt_injection_text(value):
            return ""
        return value[:DISCOVERED_TOOL_DESCRIPTION_MAX_CHARS]
    if value is None or isinstance(value, bool | int | float):
        return value
    return None


def sanitize_discovered_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    try:
        if len(json.dumps(schema, sort_keys=True)) > DISCOVERED_TOOL_SCHEMA_MAX_BYTES:
            return {"type": "object", "additionalProperties": True}
    except (TypeError, ValueError):
        return {"type": "object", "additionalProperties": True}
    sanitized = _sanitize_discovered_schema_value(schema)
    if not isinstance(sanitized, dict):
        return None
    try:
        if len(json.dumps(sanitized, sort_keys=True)) > DISCOVERED_TOOL_SCHEMA_MAX_BYTES:
            return {"type": "object", "additionalProperties": True}
    except (TypeError, ValueError):
        return {"type": "object", "additionalProperties": True}
    return sanitized


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
