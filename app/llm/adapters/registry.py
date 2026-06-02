"""Adapter registry for LLM provider routing and capability checks."""

from __future__ import annotations

from collections.abc import Callable

from app.config.settings import settings
from app.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.llm.adapters.gemini_adapter import GeminiAdapter
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.service import LLMAdapter

_ADAPTER_FACTORIES: dict[str, Callable[[], LLMAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
}

_PROVIDER_ENABLE_CHECKS: dict[str, Callable[[], bool]] = {
    "openai": lambda: settings.LLM_PROVIDER_OPENAI_ENABLED,
    "anthropic": lambda: settings.LLM_PROVIDER_ANTHROPIC_ENABLED,
    "gemini": lambda: settings.LLM_PROVIDER_GEMINI_ENABLED,
}


def get_adapter(provider: str) -> LLMAdapter:
    """Returns a provider adapter instance or raises ValueError if unsupported."""
    try:
        return _ADAPTER_FACTORIES[provider]()
    except KeyError as exc:
        raise ValueError(f"Unsupported provider: {provider}") from exc


def is_provider_enabled(provider: str) -> bool:
    """Returns true if provider is enabled in current gateway settings."""
    check = _PROVIDER_ENABLE_CHECKS.get(provider)
    if not check:
        return False
    return check()
