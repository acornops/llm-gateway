from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.examples import (
    EXAMPLE_RUN_ID,
    EXAMPLE_SESSION_ID,
    EXAMPLE_TARGET_ID,
    EXAMPLE_WORKSPACE_ID,
)
from app.llm.adapters import gemini_adapter
from app.llm.adapters.gemini_adapter import (
    GeminiAdapter,
    _close_client,
    _extract_function_calls,
    _function_call_args,
)
from app.llm.service import NormalizedLLMRequest
from app.resilience.outbound import CircuitOpenError


def _request() -> NormalizedLLMRequest:
    return NormalizedLLMRequest(
        run_id=EXAMPLE_RUN_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        session_id=EXAMPLE_SESSION_ID,
        provider="gemini",
        model="gemini-2.0-flash",
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "name": "get_weather",
                "description": "Get weather.",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                },
            }
        ],
        temperature=0.2,
        max_output_tokens=128,
    )


async def _chunk_stream():
    yield SimpleNamespace(
        text="Hello",
        function_calls=[],
        usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=2),
    )
    yield SimpleNamespace(
        text=None,
        function_calls=[SimpleNamespace(name="get_weather", args={"location": "SF"})],
        usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=4),
    )


@pytest.mark.anyio
async def test_gemini_adapter_uses_google_genai_stream(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    class FakeModels:
        async def generate_content_stream(self, **kwargs):
            calls.append(kwargs)
            return _chunk_stream()

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.aio = SimpleNamespace(models=FakeModels())
            self.closed = False

        def close(self):
            self.closed = True

    fake_client: FakeClient | None = None

    def build_client(api_key: str) -> FakeClient:
        nonlocal fake_client
        fake_client = FakeClient(api_key)
        return fake_client

    monkeypatch.setattr(gemini_adapter.genai, "Client", build_client)
    monkeypatch.setattr(
        gemini_adapter.dependency_circuit_breaker,
        "before_call",
        AsyncMock(),
    )
    monkeypatch.setattr(
        gemini_adapter.dependency_circuit_breaker,
        "record_success",
        AsyncMock(),
    )

    events = [event async for event in GeminiAdapter().stream(_request(), "gemini-key")]

    assert fake_client is not None
    assert fake_client.api_key == "gemini-key"
    assert fake_client.closed is True
    assert calls[0]["model"] == "gemini-2.0-flash"
    assert calls[0]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]

    config = calls[0]["config"]
    assert config.max_output_tokens == 128
    assert config.temperature == 0.2
    assert config.tools[0].function_declarations[0].name == "get_weather"
    assert config.tool_config.function_calling_config.mode.value == "AUTO"

    assert [event.type for event in events] == ["delta", "tool_call", "final"]
    assert events[0].text == "Hello"
    assert events[1].tool == "get_weather"
    assert events[1].arguments == {"location": "SF"}
    assert events[2].usage == {
        "input_tokens": 5,
        "output_tokens": 4,
        "tool_calls": 1,
    }


def test_extract_function_calls_reads_candidate_parts():
    chunk = SimpleNamespace(
        function_calls=None,
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(function_call=None),
                        SimpleNamespace(
                            function_call=SimpleNamespace(name="lookup", args={"region": "us"})
                        ),
                    ]
                )
            )
        ],
    )

    function_calls = _extract_function_calls(chunk)

    assert len(function_calls) == 1
    assert function_calls[0].name == "lookup"


def test_function_call_args_defaults_to_empty_mapping():
    assert _function_call_args(SimpleNamespace(args=None)) == {}


@pytest.mark.anyio
async def test_close_client_awaits_async_close():
    class AsyncCloseClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    client = AsyncCloseClient()

    await _close_client(client)

    assert client.closed is True


@pytest.mark.anyio
async def test_gemini_adapter_retries_retryable_failures_before_stream_starts(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []

    async def final_stream():
        yield SimpleNamespace(
            text=None,
            function_calls=[],
            usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=3),
        )

    class FakeModels:
        async def generate_content_stream(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("temporary gemini failure")
            return final_stream()

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.aio = SimpleNamespace(models=FakeModels())

        async def close(self):
            return None

    monkeypatch.setattr(gemini_adapter.genai, "Client", FakeClient)
    monkeypatch.setattr(gemini_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    monkeypatch.setattr(
        gemini_adapter.dependency_circuit_breaker,
        "record_failure",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        gemini_adapter.dependency_circuit_breaker,
        "record_success",
        AsyncMock(),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(gemini_adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(gemini_adapter.settings, "PROVIDER_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(gemini_adapter.settings, "PROVIDER_RETRY_BACKOFF_MS", 1)

    events = [
        event.model_dump(exclude_none=True)
        async for event in GeminiAdapter().stream(_request(), "key")
    ]

    assert len(calls) == 2
    assert events == [
        {
            "type": "final",
            "usage": {"input_tokens": 2, "output_tokens": 3, "tool_calls": 0},
        }
    ]
    sleep.assert_awaited_once()


@pytest.mark.anyio
async def test_gemini_adapter_returns_retryable_error_when_circuit_is_open(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.aio = SimpleNamespace(models=SimpleNamespace())

        def close(self):
            return None

    monkeypatch.setattr(gemini_adapter.genai, "Client", FakeClient)
    monkeypatch.setattr(
        gemini_adapter.dependency_circuit_breaker,
        "before_call",
        AsyncMock(side_effect=CircuitOpenError("provider", "gemini", 250)),
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in GeminiAdapter().stream(_request(), "key")
    ]

    assert events == [
        {
            "type": "error",
            "code": "GEMINI_ERROR",
            "message": "Provider temporarily unavailable",
            "retryable": True,
        }
    ]
