import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.llm.adapters.common import (
    alternate_openai_output_token_param,
    resolve_openai_output_token_param,
    should_retry_openai_with_alt_token_param,
    should_retry_openai_without_temperature,
    supports_openai_custom_temperature,
)
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.service import NormalizedLLMRequest
from app.resilience.outbound import CircuitOpenError

OPENAI_STREAM_REPLAY_FIXTURES = json.loads(
    (
        Path(__file__).resolve().parent
        / "fixtures"
        / "openai_stream_replays.json"
    ).read_text()
)

def _build_request(model: str) -> NormalizedLLMRequest:
    return NormalizedLLMRequest(
        run_id="11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
        target_id="33333333-3333-4333-8333-333333333333",
        target_type="kubernetes",
        session_id="44444444-4444-4444-8444-444444444444",
        provider="openai",
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        max_output_tokens=128,
    )


def _to_namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _normalize_chunk_fixture(chunk: dict) -> dict:
    normalized = copy.deepcopy(chunk)
    normalized.setdefault("usage", None)
    normalized.setdefault("choices", [])
    for choice in normalized["choices"]:
        choice.setdefault("finish_reason", None)
        delta = choice.setdefault("delta", {})
        delta.setdefault("content", None)
        delta.setdefault("tool_calls", None)
    return normalized


def test_openai_output_token_param_resolution() -> None:
    assert resolve_openai_output_token_param("gpt-4o") == "max_tokens"
    assert resolve_openai_output_token_param("o4-mini") == "max_completion_tokens"
    assert resolve_openai_output_token_param("gpt-5-mini") == "max_completion_tokens"
    assert supports_openai_custom_temperature("gpt-4o") is True
    assert supports_openai_custom_temperature("gpt-5-nano") is False
    assert alternate_openai_output_token_param("max_tokens") == "max_completion_tokens"
    assert alternate_openai_output_token_param("max_completion_tokens") == "max_tokens"


def test_retry_detection_for_openai_token_param_error() -> None:
    message = (
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead."
    )
    assert should_retry_openai_with_alt_token_param(message, "max_tokens") is True
    assert should_retry_openai_with_alt_token_param("random failure", "max_tokens") is False
    temperature_error = (
        "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported."
    )
    assert should_retry_openai_without_temperature(temperature_error, True) is True
    assert should_retry_openai_without_temperature(temperature_error, False) is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    OPENAI_STREAM_REPLAY_FIXTURES,
    ids=[case["name"] for case in OPENAI_STREAM_REPLAY_FIXTURES],
)
async def test_openai_adapter_replays_stream_contract_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    case: dict,
) -> None:
    async def stream_response():
        for chunk in case["chunks"]:
            yield _to_namespace(_normalize_chunk_fixture(chunk))

    class FakeCompletions:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    events = [
        event.model_dump(exclude_none=True)
        async for event in adapter.stream(_build_request(case["model"]), "fake-key")
    ]

    assert events == case["expected_events"]


@pytest.mark.anyio
async def test_openai_adapter_uses_max_completion_tokens_for_o_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[],
        )

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str):
            del api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("o4-mini"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 1
    assert "max_completion_tokens" in calls[0]
    assert "max_tokens" not in calls[0]


@pytest.mark.anyio
async def test_openai_adapter_retries_with_alternate_token_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeBadRequestError(Exception):
        pass

    async def stream_response():
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
            choices=[],
        )

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeBadRequestError(
                    "Unsupported parameter: 'max_tokens' is not supported with this model. "
                    "Use 'max_completion_tokens' instead."
                )
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str):
            del api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("app.llm.adapters.openai_adapter.BadRequestError", FakeBadRequestError)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("gpt-4o-mini"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 2
    assert "max_tokens" in calls[0]
    assert "max_completion_tokens" in calls[1]


@pytest.mark.anyio
async def test_openai_adapter_retries_transient_connection_error_before_stream_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[],
        )

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise httpx.ConnectError("temporary connection failure")
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str):
            del api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("app.llm.adapters.openai_adapter.settings.PROVIDER_RETRY_BACKOFF_MS", 1)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("gpt-4o-mini"), "fake-key")]

    assert any(event.type == "final" for event in events)
    assert len(calls) == 2


@pytest.mark.anyio
async def test_openai_adapter_returns_sanitized_error_when_circuit_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, api_key: str):
            del api_key
            self.chat = SimpleNamespace(completions=SimpleNamespace())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr(
        "app.llm.adapters.openai_adapter.dependency_circuit_breaker.before_call",
        AsyncMock(side_effect=CircuitOpenError("provider", "openai", 250)),
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIAdapter().stream(_build_request("gpt-4o-mini"), "fake-key")
    ]

    assert events == [
        {
            "type": "error",
            "code": "OPENAI_ERROR",
            "message": "Provider temporarily unavailable",
            "retryable": True,
        }
    ]


@pytest.mark.anyio
async def test_openai_adapter_omits_temperature_for_gpt5_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[],
        )

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str):
            del api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("gpt-5-nano"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 1
    assert "temperature" not in calls[0]
    assert "max_completion_tokens" in calls[0]


@pytest.mark.anyio
async def test_openai_adapter_retries_without_temperature_when_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeBadRequestError(Exception):
        pass

    async def stream_response():
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
            choices=[],
        )

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeBadRequestError(
                    "Unsupported value: 'temperature' does not support 0.2 with this model. "
                    "Only the default (1) value is supported."
                )
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str):
            del api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("app.llm.adapters.openai_adapter.BadRequestError", FakeBadRequestError)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("gpt-4o-mini"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]
