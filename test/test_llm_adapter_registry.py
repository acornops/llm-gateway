import pytest

from app.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.llm.adapters.gemini_adapter import GeminiAdapter
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.adapters.registry import get_adapter, is_provider_enabled


def test_get_adapter_returns_provider_specific_adapter():
    assert isinstance(get_adapter("openai"), OpenAIAdapter)
    assert isinstance(get_adapter("anthropic"), AnthropicAdapter)
    assert isinstance(get_adapter("gemini"), GeminiAdapter)


def test_get_adapter_rejects_unsupported_provider():
    with pytest.raises(ValueError, match="Unsupported provider: unknown"):
        get_adapter("unknown")


def test_is_provider_enabled_uses_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.llm.adapters.registry.settings.LLM_PROVIDER_OPENAI_ENABLED", True)
    monkeypatch.setattr("app.llm.adapters.registry.settings.LLM_PROVIDER_ANTHROPIC_ENABLED", False)
    monkeypatch.setattr("app.llm.adapters.registry.settings.LLM_PROVIDER_GEMINI_ENABLED", True)

    assert is_provider_enabled("openai") is True
    assert is_provider_enabled("anthropic") is False
    assert is_provider_enabled("gemini") is True
    assert is_provider_enabled("unknown") is False
