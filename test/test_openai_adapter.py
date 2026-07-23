import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.llm.adapters.common import (
    build_openai_response_tools,
    should_retry_openai_without_reasoning,
    should_retry_openai_without_temperature,
    supports_openai_custom_temperature,
)
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.adapters.openai_chat_completions_adapter import (
    OpenAIChatCompletionsAdapter,
)
from app.llm.service import NormalizedLLMRequest, ReasoningConfig, ToolSpec
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


def test_openai_output_token_param_resolution() -> None:
    assert supports_openai_custom_temperature("gpt-4o") is True
    assert supports_openai_custom_temperature("gpt-5-nano") is False


def test_openai_preserves_platform_function_alias() -> None:
    tools = build_openai_response_tools([
        ToolSpec(name="acornops_generate_pdf_report", description="Generate report.")
    ])
    assert tools[0]["name"] == "acornops_generate_pdf_report"


@pytest.mark.parametrize("name", ["reports.pdf.generate", "9invalid", "bad name", "a" * 64])
def test_gateway_rejects_provider_unsafe_function_names(name: str) -> None:
    with pytest.raises(ValueError, match="function name must match"):
        ToolSpec(name=name)


def test_retry_detection_for_openai_temperature_error() -> None:
    temperature_error = (
        "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported."
    )
    assert should_retry_openai_without_temperature(temperature_error, True) is True
    assert should_retry_openai_without_temperature(temperature_error, False) is False


def test_retry_detection_for_openai_reasoning_error() -> None:
    assert should_retry_openai_without_reasoning("Unsupported parameter: 'reasoning'.", True)
    assert should_retry_openai_without_reasoning("This model does not support reasoning.", True)
    assert not should_retry_openai_without_reasoning("Invalid request: bad input.", True)
    assert not should_retry_openai_without_reasoning("Unsupported parameter: 'reasoning'.", False)


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
            yield _to_namespace(chunk)

    class FakeResponses:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    events = [
        event.model_dump(exclude_none=True)
        async for event in adapter.stream(_build_request(case["model"]), "fake-key")
    ]

    assert events == case["expected_events"]


@pytest.mark.anyio
async def test_openai_adapter_uses_responses_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("o4-mini"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 1
    assert calls[0]["max_output_tokens"] == 128
    assert "max_tokens" not in calls[0]
    assert "max_completion_tokens" not in calls[0]


@pytest.mark.anyio
async def test_openai_adapter_uses_configured_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client_kwargs: dict[str, object] = {}
    http_client = object()

    async def stream_response():
        yield SimpleNamespace(type="response.completed")

    class FakeResponses:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr(
        "app.llm.adapters.openai_adapter.provider_http_client",
        lambda _provider: http_client,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_adapter.settings.LLM_PROVIDER_OPENAI_BASE_URL",
        "https://openai.internal/v1",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient-ignored.internal/v1")

    _ = [event async for event in OpenAIAdapter().stream(_build_request("gpt-4o"), "key")]

    assert client_kwargs == {
        "api_key": "key",
        "base_url": "https://openai.internal/v1",
        "http_client": http_client,
    }


@pytest.mark.anyio
async def test_openai_adapter_maps_native_web_search_domain_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    payload = _build_request("gpt-4o-mini").model_dump()
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

    events = [event async for event in OpenAIAdapter().stream(req, "fake-key")]

    assert any(event.type == "final" for event in events)
    assert calls[0]["tools"] == [
        {
            "type": "web_search",
            "filters": {
                "allowed_domains": ["docs.example.com"],
                "blocked_domains": ["ads.example.com"],
            },
        }
    ]
    assert calls[0]["tool_choice"] == "auto"


@pytest.mark.anyio
async def test_openai_adapter_maps_reasoning_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            type="response.reasoning_summary_text.delta",
            delta="Checked cluster status.",
        )
        yield SimpleNamespace(
            type="response.reasoning_text.delta",
            delta="raw private chain-of-thought",
        )
        yield SimpleNamespace(
            type="response.reasoning_summary_text.done",
            text="Checked cluster status.",
        )
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=12,
                    output_tokens=7,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=3),
                ),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    req = _build_request("gpt-4o-mini").model_copy(
        update={
            "reasoning": ReasoningConfig(summary_mode="auto", effort="low"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in adapter.stream(req, "fake-key")
    ]

    assert calls[0]["reasoning"] == {"summary": "auto", "effort": "low"}
    assert events == [
        {
            "type": "reasoning_summary_delta",
            "text": "Checked cluster status.",
            "provider": "openai",
        },
        {
            "type": "reasoning_summary_completed",
            "text": "Checked cluster status.",
            "provider": "openai",
        },
        {
            "type": "final",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "tool_calls": 0,
                "reasoning_tokens": 3,
            },
        },
    ]


@pytest.mark.anyio
async def test_openai_adapter_sends_reasoning_effort_without_streaming_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            type="response.reasoning_summary_text.delta",
            delta="hidden summary",
        )
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    req = _build_request("gpt-5-mini").model_copy(
        update={
            "reasoning": ReasoningConfig(summary_mode="off", effort="high"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIAdapter().stream(req, "fake-key")
    ]

    assert calls[0]["reasoning"] == {"effort": "high"}
    assert events == [
        {
            "type": "final",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "tool_calls": 0,
            },
        },
    ]


@pytest.mark.anyio
async def test_openai_adapter_maps_reasoning_summary_part_done_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stream_response():
        yield SimpleNamespace(
            type="response.reasoning_summary_text.done",
            item_id="rs_1",
            output_index=0,
            summary_index=0,
            text="Checked cluster status.",
        )
        yield SimpleNamespace(
            type="response.reasoning_summary_part.done",
            item_id="rs_1",
            output_index=0,
            summary_index=0,
            part=SimpleNamespace(type="summary_text", text="Checked cluster status."),
        )
        yield SimpleNamespace(
            type="response.reasoning_summary_part.done",
            item_id="rs_1",
            output_index=0,
            summary_index=1,
            part=SimpleNamespace(type="summary_text", text="Reviewed recent rollout events."),
        )
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            ),
        )

    class FakeResponses:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    req = _build_request("gpt-4o-mini").model_copy(
        update={
            "reasoning": ReasoningConfig(summary_mode="auto", effort="off"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIAdapter().stream(req, "fake-key")
    ]

    assert events == [
        {
            "type": "reasoning_summary_completed",
            "text": "Checked cluster status.",
            "provider": "openai",
        },
        {
            "type": "reasoning_summary_completed",
            "text": "Reviewed recent rollout events.",
            "provider": "openai",
        },
        {
            "type": "final",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "tool_calls": 0,
            },
        },
    ]


@pytest.mark.anyio
async def test_openai_adapter_retries_without_reasoning_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeBadRequestError(Exception):
        pass

    async def stream_response():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeBadRequestError("Unsupported parameter: 'reasoning'.")
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("app.llm.adapters.openai_adapter.BadRequestError", FakeBadRequestError)

    req = _build_request("gpt-4o-mini").model_copy(
        update={
            "reasoning": ReasoningConfig(summary_mode="auto", effort="off"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIAdapter().stream(req, "fake-key")
    ]

    assert len(calls) == 2
    assert calls[0]["reasoning"] == {"summary": "auto"}
    assert "reasoning" not in calls[1]
    assert events == [
        {
            "type": "reasoning_summary_unavailable",
            "provider": "openai",
            "reason": "unsupported_model",
        },
        {
            "type": "final",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "tool_calls": 0,
            },
        },
    ]


@pytest.mark.anyio
async def test_openai_adapter_does_not_degrade_unrelated_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeBadRequestError(Exception):
        pass

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            raise FakeBadRequestError("Invalid request: malformed tool schema.")

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("app.llm.adapters.openai_adapter.BadRequestError", FakeBadRequestError)

    req = _build_request("gpt-4o-mini").model_copy(
        update={
            "reasoning": ReasoningConfig(summary_mode="auto", effort="off"),
        }
    )
    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIAdapter().stream(req, "fake-key")
    ]

    assert len(calls) == 1
    assert calls[0]["reasoning"] == {"summary": "auto"}
    assert events == [
        {
            "type": "error",
            "code": "OPENAI_ERROR",
            "message": "Provider request failed",
            "retryable": False,
        }
    ]


@pytest.mark.anyio
async def test_openai_adapter_retries_transient_connection_error_before_stream_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise httpx.ConnectError("temporary connection failure")
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    log_failure = Mock()
    monkeypatch.setattr(
        "app.llm.adapters.openai_adapter.log_provider_stream_failure",
        log_failure,
    )
    monkeypatch.setattr("app.llm.adapters.openai_adapter.settings.PROVIDER_RETRY_BACKOFF_MS", 1)

    adapter = OpenAIAdapter()
    adapter_request = _build_request("gpt-4o-mini")
    events = [event async for event in adapter.stream(adapter_request, "fake-key")]

    assert any(event.type == "final" for event in events)
    assert len(calls) == 2
    assert log_failure.call_args.kwargs["provider"] == "openai"
    assert log_failure.call_args.kwargs["model"] == "gpt-4o-mini"
    assert log_failure.call_args.kwargs["run_id"] == adapter_request.run_id
    assert log_failure.call_args.kwargs["workspace_id"] == adapter_request.workspace_id


@pytest.mark.anyio
async def test_openai_adapter_returns_sanitized_error_when_circuit_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = SimpleNamespace()

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
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("gpt-5-nano"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 1
    assert "temperature" not in calls[0]
    assert calls[0]["max_output_tokens"] == 128


@pytest.mark.anyio
async def test_openai_adapter_retries_without_temperature_when_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeBadRequestError(Exception):
        pass

    async def stream_response():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            ),
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeBadRequestError(
                    "Unsupported value: 'temperature' does not support 0.2 with this model. "
                    "Only the default (1) value is supported."
                )
            return stream_response()

    class FakeClient:
        def __init__(self, api_key: str, **_kwargs):
            del api_key
            self.responses = FakeResponses()

    monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("app.llm.adapters.openai_adapter.BadRequestError", FakeBadRequestError)

    adapter = OpenAIAdapter()
    events = [event async for event in adapter.stream(_build_request("gpt-4o-mini"), "fake-key")]
    assert any(event.type == "final" for event in events)
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]


@pytest.mark.anyio
async def test_openai_adapter_routes_to_chat_completions_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_chat_stream(self, req, api_key):
        del self, req, api_key
        yield SimpleNamespace(type="delta", text="chat")

    monkeypatch.setattr(
        "app.llm.adapters.openai_adapter.settings.LLM_PROVIDER_OPENAI_API_SURFACE",
        "chat_completions",
    )
    monkeypatch.setattr(OpenAIChatCompletionsAdapter, "stream", fake_chat_stream)

    events = [
        event
        async for event in OpenAIAdapter().stream(_build_request("gpt-4o-mini"), "key")
    ]

    assert [(event.type, event.text) for event in events] == [("delta", "chat")]
