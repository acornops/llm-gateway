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
from app.llm.adapters import anthropic_adapter
from app.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.llm.service import NormalizedLLMRequest, ReasoningConfig
from app.resilience.outbound import CircuitOpenError


def _request(*, include_tools: bool = True) -> NormalizedLLMRequest:
    tools = []
    if include_tools:
        tools = [
            {
                "name": "get_weather",
                "description": "Get weather.",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                },
            }
        ]

    return NormalizedLLMRequest(
        run_id=EXAMPLE_RUN_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        session_id=EXAMPLE_SESSION_ID,
        provider="anthropic",
        model="claude-3-7-sonnet",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hello"},
        ],
        tools=tools,
        temperature=0.2,
        max_output_tokens=128,
    )


class FakeStreamContext:
    def __init__(
        self,
        *,
        events: list[SimpleNamespace] | None = None,
        error: Exception | None = None,
    ):
        self._events = events or []
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event


class FakeMessages:
    def __init__(self, contexts: list[FakeStreamContext]):
        self._contexts = list(contexts)
        self.calls: list[dict[str, object]] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._contexts.pop(0)


@pytest.mark.anyio
async def test_anthropic_adapter_streams_text_tool_calls_and_usage(monkeypatch: pytest.MonkeyPatch):
    messages = FakeMessages(
        [
            FakeStreamContext(
                events=[
                    SimpleNamespace(type="text", text="Hello"),
                    SimpleNamespace(
                        type="content_block_start",
                        index=0,
                        content_block=SimpleNamespace(
                            type="tool_use",
                            id="call-1",
                            name="get_weather",
                        ),
                    ),
                    SimpleNamespace(
                        type="content_block_delta",
                        index=0,
                        delta=SimpleNamespace(
                            type="input_json_delta",
                            partial_json='{"location":"SF"}',
                        ),
                    ),
                    SimpleNamespace(type="content_block_stop", index=0),
                    SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
                    ),
                ]
            )
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    record_success = AsyncMock()
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_success",
        record_success,
    )

    events = [event async for event in AnthropicAdapter().stream(_request(), "anthropic-key")]

    assert messages.calls == [
        {
            "model": "claude-3-7-sonnet",
            "messages": [{"role": "user", "content": "hello"}],
            "system": "Be concise.",
            "max_tokens": 128,
            "temperature": 0.2,
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                    },
                }
            ],
        }
    ]
    assert [event.model_dump(exclude_none=True) for event in events] == [
        {"type": "delta", "text": "Hello"},
        {
            "type": "tool_call",
            "call_id": "call-1",
            "tool": "get_weather",
            "arguments": {"location": "SF"},
        },
        {
            "type": "final",
            "usage": {"input_tokens": 5, "output_tokens": 7, "tool_calls": 1},
        },
    ]
    record_success.assert_awaited_once_with("provider:anthropic")


@pytest.mark.anyio
async def test_anthropic_adapter_uses_configured_base_url(monkeypatch: pytest.MonkeyPatch):
    client_kwargs: dict[str, object] = {}
    messages = FakeMessages([FakeStreamContext(events=[])])

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "record_success", AsyncMock())
    monkeypatch.setattr(
        anthropic_adapter.settings,
        "LLM_PROVIDER_ANTHROPIC_BASE_URL",
        "https://anthropic.internal",
    )

    _ = [event async for event in AnthropicAdapter().stream(_request(), "key")]

    assert client_kwargs == {"api_key": "key", "base_url": "https://anthropic.internal"}


@pytest.mark.anyio
async def test_anthropic_adapter_maps_native_web_search_domain_filters(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = FakeMessages(
        [
            FakeStreamContext(
                events=[
                    SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    )
                ]
            )
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_success",
        AsyncMock(),
    )

    payload = _request(include_tools=False).model_dump()
    payload["native_tools"] = [
        {
            "id": "web_search",
            "config": {
                "domainFilters": {
                    "allowedDomains": ["docs.example.com"],
                    "blockedDomains": ["ads.example.com"],
                }
            },
        }
    ]
    req = NormalizedLLMRequest(**payload)

    events = [event async for event in AnthropicAdapter().stream(req, "anthropic-key")]

    assert [event.type for event in events] == ["final"]
    assert messages.calls[0]["tools"] == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "allowed_domains": ["docs.example.com"],
            "blocked_domains": ["ads.example.com"],
        }
    ]


@pytest.mark.anyio
async def test_anthropic_adapter_uses_adaptive_thinking_for_current_adaptive_models(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = FakeMessages(
        [
            FakeStreamContext(
                events=[
                    SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    ),
                ]
            )
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_success",
        AsyncMock(),
    )

    req = _request(include_tools=False).model_copy(
        update={
            "model": "claude-fable-5",
            "reasoning": ReasoningConfig(summary_mode="auto", effort="high"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in AnthropicAdapter().stream(req, "anthropic-key")
    ]

    assert messages.calls[0]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "effort": "high",
    }
    assert events == [
        {
            "type": "reasoning_summary_unavailable",
            "provider": "anthropic",
            "reason": "provider_omitted",
        },
        {
            "type": "final",
            "usage": {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0},
        },
    ]


@pytest.mark.anyio
async def test_anthropic_adapter_sends_thinking_effort_without_streaming_summaries(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = FakeMessages(
        [
            FakeStreamContext(
                events=[
                    SimpleNamespace(
                        type="content_block_delta",
                        index=0,
                        delta=SimpleNamespace(type="thinking_delta", thinking="hidden summary"),
                    ),
                    SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    ),
                ]
            )
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_success",
        AsyncMock(),
    )

    req = _request(include_tools=False).model_copy(
        update={"reasoning": ReasoningConfig(summary_mode="off", effort="high")}
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in AnthropicAdapter().stream(req, "anthropic-key")
    ]

    assert messages.calls[0]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 127,
    }
    assert events == [
        {
            "type": "final",
            "usage": {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0},
        }
    ]


@pytest.mark.anyio
async def test_anthropic_adapter_omits_thinking_when_token_budget_cannot_support_it(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = FakeMessages(
        [
            FakeStreamContext(
                events=[
                    SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    ),
                ]
            )
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_success",
        AsyncMock(),
    )

    req = _request(include_tools=False).model_copy(
        update={
            "max_output_tokens": 1,
            "reasoning": ReasoningConfig(summary_mode="auto", effort="high"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in AnthropicAdapter().stream(req, "anthropic-key")
    ]

    assert "thinking" not in messages.calls[0]
    assert events == [
        {
            "type": "reasoning_summary_unavailable",
            "provider": "anthropic",
            "reason": "provider_omitted",
        },
        {
            "type": "final",
            "usage": {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0},
        },
    ]


@pytest.mark.anyio
async def test_anthropic_adapter_retries_retryable_failures_before_stream_starts(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = FakeMessages(
        [
            FakeStreamContext(error=httpx.ConnectTimeout("temporary failure")),
            FakeStreamContext(
                events=[
                    SimpleNamespace(
                        type="message_delta",
                        usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                    )
                ]
            ),
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = messages

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(anthropic_adapter.dependency_circuit_breaker, "before_call", AsyncMock())
    record_failure = AsyncMock(return_value=False)
    record_success = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_failure",
        record_failure,
    )
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "record_success",
        record_success,
    )
    monkeypatch.setattr(anthropic_adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(anthropic_adapter.settings, "PROVIDER_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(anthropic_adapter.settings, "PROVIDER_RETRY_BACKOFF_MS", 1)

    events = [
        event.model_dump(exclude_none=True)
        async for event in AnthropicAdapter().stream(_request(include_tools=False), "anthropic-key")
    ]

    assert len(messages.calls) == 2
    assert events == [
        {
            "type": "final",
            "usage": {"input_tokens": 2, "output_tokens": 3, "tool_calls": 0},
        }
    ]
    record_failure.assert_awaited_once_with(
        "provider:anthropic",
        anthropic_adapter.settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        anthropic_adapter.settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
    )
    sleep.assert_awaited_once()
    record_success.assert_awaited_once_with("provider:anthropic")


@pytest.mark.anyio
async def test_anthropic_adapter_returns_retryable_error_when_circuit_is_open(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.messages = FakeMessages([])

    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(
        anthropic_adapter.dependency_circuit_breaker,
        "before_call",
        AsyncMock(side_effect=CircuitOpenError("provider", "anthropic", 250)),
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in AnthropicAdapter().stream(_request(include_tools=False), "anthropic-key")
    ]

    assert events == [
        {
            "type": "error",
            "code": "ANTHROPIC_ERROR",
            "message": "Provider temporarily unavailable",
            "retryable": True,
        }
    ]
