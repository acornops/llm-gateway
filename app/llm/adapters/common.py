"""Common adapter helpers for provider-specific request/response handling."""

from __future__ import annotations

import json
from typing import Any

from app.llm.service import NativeToolSpec, ToolSpec

_OPENAI_DEFAULT_TEMPERATURE_ONLY_MODELS: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5")


def supports_openai_custom_temperature(model: str) -> bool:
    """Returns false for model families that only support default temperature."""
    normalized = model.strip().lower()
    return not any(
        normalized.startswith(prefix)
        for prefix in _OPENAI_DEFAULT_TEMPERATURE_ONLY_MODELS
    )


def should_retry_openai_without_temperature(error_message: str, temperature_sent: bool) -> bool:
    """Returns true when OpenAI rejects the provided temperature parameter/value."""
    if not temperature_sent:
        return False

    normalized = error_message.lower()
    mentions_temperature = "temperature" in normalized
    if not mentions_temperature:
        return False

    return (
        "unsupported value" in normalized
        or "unsupported parameter" in normalized
        or "does not support" in normalized
        or "only the default (1) value is supported" in normalized
        or "only the default value is supported" in normalized
    )


def should_retry_openai_without_reasoning(error_message: str, reasoning_sent: bool) -> bool:
    """Returns true when OpenAI rejects the reasoning request shape or model support."""
    if not reasoning_sent:
        return False

    normalized = error_message.lower()
    mentions_reasoning = "reasoning" in normalized
    if not mentions_reasoning:
        return False

    return (
        "unsupported parameter" in normalized
        or "unknown parameter" in normalized
        or "does not support" in normalized
        or "not supported" in normalized
        or "unsupported_model" in normalized
    )


def sanitize_gemini_schema(value: Any, in_properties: bool = False) -> Any:
    """Sanitizes JSON schema for Gemini function declarations."""
    allowed_schema_fields = {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
    }

    if isinstance(value, dict):
        if in_properties:
            return {
                key: sanitize_gemini_schema(nested)
                for key, nested in value.items()
                if isinstance(nested, dict)
            }

        cleaned: dict[str, Any] = {}
        for key, nested in value.items():
            if key not in allowed_schema_fields:
                continue
            if key == "properties":
                cleaned[key] = sanitize_gemini_schema(nested, in_properties=True)
            else:
                cleaned[key] = sanitize_gemini_schema(nested)
        return cleaned

    if isinstance(value, list):
        return [sanitize_gemini_schema(item, in_properties=in_properties) for item in value]

    return value


def _web_search_domain_filters(native_tool: NativeToolSpec) -> tuple[list[str], list[str]]:
    domain_filters = native_tool.config.get("domainFilters") or {}
    if not isinstance(domain_filters, dict):
        return [], []
    allowed = domain_filters.get("allowedDomains") or []
    blocked = domain_filters.get("blockedDomains") or []
    return (
        list(allowed) if isinstance(allowed, list) else [],
        list(blocked) if isinstance(blocked, list) else [],
    )


def build_openai_response_tools(
    tools: list[ToolSpec],
    native_tools: list[NativeToolSpec] | None = None,
) -> list[dict[str, Any]]:
    """Builds OpenAI Responses API function tool declarations."""
    declarations = [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description or f"Execute tool '{tool.name}'.",
            "parameters": tool.input_schema or {"type": "object", "additionalProperties": True},
        }
        for tool in tools
    ]
    for native_tool in native_tools or []:
        if native_tool.id != "web_search":
            continue
        web_search: dict[str, Any] = {"type": "web_search"}
        allowed, blocked = _web_search_domain_filters(native_tool)
        filters: dict[str, Any] = {}
        if allowed:
            filters["allowed_domains"] = allowed
        if blocked:
            filters["blocked_domains"] = blocked
        if filters:
            web_search["filters"] = filters
        declarations.append(web_search)
    return declarations


def build_openai_chat_completion_tools(
    tools: list[ToolSpec],
) -> list[dict[str, Any]]:
    """Builds OpenAI Chat Completions function tool declarations."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or f"Execute tool '{tool.name}'.",
                "parameters": tool.input_schema
                or {"type": "object", "additionalProperties": True},
            },
        }
        for tool in tools
    ]


def parse_openai_tool_arguments(value: str) -> dict[str, Any] | None:
    """Parses provider tool arguments, returning None for unsafe shapes."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_anthropic_tools(
    tools: list[ToolSpec],
    native_tools: list[NativeToolSpec] | None = None,
) -> list[dict[str, Any]]:
    """Builds Anthropic Messages API tool declarations."""
    declarations = [
        {
            "name": tool.name,
            "description": tool.description or f"Execute tool '{tool.name}'.",
            "input_schema": tool.input_schema or {"type": "object", "additionalProperties": True},
        }
        for tool in tools
    ]
    for native_tool in native_tools or []:
        if native_tool.id != "web_search":
            continue
        web_search: dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
        }
        allowed, blocked = _web_search_domain_filters(native_tool)
        if allowed:
            web_search["allowed_domains"] = allowed
        if blocked:
            web_search["blocked_domains"] = blocked
        declarations.append(web_search)
    return declarations


def build_gemini_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Builds Gemini function declaration payload from tool specs."""
    return [
        {
            "function_declarations": [
                {
                    "name": tool.name,
                    "description": tool.description or f"Execute tool '{tool.name}'.",
                    "parameters": sanitize_gemini_schema(tool.input_schema) or {"type": "object"},
                }
                for tool in tools
            ]
        }
    ]
