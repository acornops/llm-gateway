import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.llm.adapters.common import build_openai_chat_completion_tools
from app.llm.adapters.openai_chat_completions_adapter import (
    OpenAIChatCompletionsAdapter,
)
from app.llm.service import NativeToolSpec, NormalizedLLMRequest, ReasoningConfig, ToolSpec

OPENAI_CHAT_STREAM_REPLAY_FIXTURES = json.loads(
    (
        Path(__file__).resolve().parent
        / "fixtures"
        / "openai_chat_completions_stream_replays.json"
    ).read_text()
)


def _to_namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _to_namespace(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _build_request(
    model: str = "gpt-4o-mini",
    *,
    tools: list[ToolSpec] | None = None,
    native_tools: list[NativeToolSpec] | None = None,
    reasoning: ReasoningConfig | None = None,
) -> NormalizedLLMRequest:
    return NormalizedLLMRequest(
        run_id="11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
        target_id="33333333-3333-4333-8333-333333333333",
        target_type="kubernetes",
        session_id="44444444-4444-4444-8444-444444444444",
        provider="openai",
        model=model,
        messages=[
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "hello"},
        ],
        tools=tools or [],
        native_tools=native_tools or [],
        max_output_tokens=128,
        reasoning=reasoning or ReasoningConfig(),
    )


def test_build_openai_chat_completion_tools_uses_nested_function_shape() -> None:
    tools = build_openai_chat_completion_tools(
        [
            ToolSpec(
                name="lookup_pod",
                description="Look up a pod.",
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            )
        ]
    )

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "lookup_pod",
                "description": "Look up a pod.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            },
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    OPENAI_CHAT_STREAM_REPLAY_FIXTURES,
    ids=[case["name"] for case in OPENAI_CHAT_STREAM_REPLAY_FIXTURES],
)
async def test_chat_completions_adapter_replays_stream_contract_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    case: dict,
) -> None:
    async def stream_response():
        for chunk in case["chunks"]:
            yield _to_namespace(chunk)

    class FakeCompletions:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIChatCompletionsAdapter().stream(
            _build_request(case["model"]),
            "fake-key",
        )
    ]

    assert events == case["expected_events"]


@pytest.mark.anyio
async def test_chat_completions_adapter_uses_sdk_chat_endpoint_and_sse_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[httpx.Request] = []

    async def provider_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[{"index":0,'
                '"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
                'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[{"index":0,'
                '"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[],'
                '"usage":{"prompt_tokens":3,"completion_tokens":1,'
                '"total_tokens":4}}\n\n'
                "data: [DONE]\n\n"
            ),
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_handler))
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.provider_http_client",
        lambda _provider: http_client,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.settings."
        "LLM_PROVIDER_OPENAI_BASE_URL",
        "https://provider.example/v1",
    )

    try:
        events = [
            event.model_dump(exclude_none=True)
            async for event in OpenAIChatCompletionsAdapter().stream(
                _build_request(),
                "fake-key",
            )
        ]
    finally:
        await http_client.aclose()

    assert events == [
        {"type": "delta", "text": "hello"},
        {
            "type": "final",
            "usage": {"input_tokens": 3, "output_tokens": 1, "tool_calls": 0},
        },
    ]
    assert len(captured_requests) == 1
    assert captured_requests[0].url == "https://provider.example/v1/chat/completions"
    request_body = json.loads(captured_requests[0].content)
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}
    assert request_body["max_completion_tokens"] == 128


@pytest.mark.anyio
async def test_chat_completions_adapter_uses_sdk_tool_call_delta_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def provider_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"id":"chatcmpl-tool","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[{"index":0,'
                '"delta":{"tool_calls":[{"index":0,"id":"call_lookup",'
                '"type":"function","function":{"name":"lookup_pod",'
                '"arguments":""}}]},"finish_reason":null}]}\n\n'
                'data: {"id":"chatcmpl-tool","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[{"index":0,'
                '"delta":{"tool_calls":[{"index":0,"function":{'
                '"arguments":"{\\"id\\":42}"}}]},"finish_reason":null}]}\n\n'
                'data: {"id":"chatcmpl-tool","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[{"index":0,'
                '"delta":{},"finish_reason":"tool_calls"}]}\n\n'
                'data: {"id":"chatcmpl-tool","object":"chat.completion.chunk",'
                '"created":1,"model":"gpt-4o-mini","choices":[],'
                '"usage":{"prompt_tokens":12,"completion_tokens":5,'
                '"total_tokens":17}}\n\n'
                "data: [DONE]\n\n"
            ),
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_handler))
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.provider_http_client",
        lambda _provider: http_client,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.settings."
        "LLM_PROVIDER_OPENAI_BASE_URL",
        "https://provider.example/v1",
    )

    try:
        events = [
            event.model_dump(exclude_none=True)
            async for event in OpenAIChatCompletionsAdapter().stream(
                _build_request(
                    tools=[ToolSpec(name="lookup_pod", description="Look up a pod.")]
                ),
                "fake-key",
            )
        ]
    finally:
        await http_client.aclose()

    assert events == [
        {
            "type": "tool_call",
            "call_id": "call_lookup",
            "tool": "lookup_pod",
            "arguments": {"id": 42},
        },
        {
            "type": "final",
            "usage": {"input_tokens": 12, "output_tokens": 5, "tool_calls": 1},
        },
    ]


@pytest.mark.anyio
async def test_chat_completions_adapter_maps_request_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )
    request = _build_request(
        tools=[ToolSpec(name="lookup_pod", description="Look up a pod.")],
        reasoning=ReasoningConfig(effort="high"),
    )

    events = [
        event
        async for event in OpenAIChatCompletionsAdapter().stream(request, "fake-key")
    ]

    assert any(event.type == "final" for event in events)
    assert len(calls) == 1
    assert calls[0]["messages"] == [
        {"role": "system", "content": "Be precise."},
        {"role": "user", "content": "hello"},
    ]
    assert calls[0]["max_completion_tokens"] == 128
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert calls[0]["tool_choice"] == "auto"
    assert calls[0]["tools"][0]["function"]["name"] == "lookup_pod"
    assert "max_output_tokens" not in calls[0]
    assert "max_tokens" not in calls[0]


@pytest.mark.anyio
async def test_chat_completions_adapter_omits_temperature_for_gpt5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def stream_response():
        yield SimpleNamespace(choices=[], usage=None)

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )

    _ = [
        event
        async for event in OpenAIChatCompletionsAdapter().stream(
            _build_request("gpt-5"),
            "fake-key",
        )
    ]

    assert len(calls) == 1
    assert "temperature" not in calls[0]


@pytest.mark.anyio
async def test_chat_completions_adapter_retries_without_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeBadRequestError(Exception):
        pass

    async def stream_response():
        yield SimpleNamespace(choices=[], usage=None)

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeBadRequestError(
                    "Unsupported parameter: 'reasoning_effort'."
                )
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.BadRequestError",
        FakeBadRequestError,
    )
    request = _build_request(reasoning=ReasoningConfig(effort="medium"))

    events = [
        event
        async for event in OpenAIChatCompletionsAdapter().stream(request, "fake-key")
    ]

    assert any(event.type == "final" for event in events)
    assert len(calls) == 2
    assert calls[0]["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in calls[1]


@pytest.mark.anyio
async def test_chat_completions_adapter_reports_summary_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stream_response():
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    class FakeCompletions:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )
    request = _build_request(reasoning=ReasoningConfig(summary_mode="auto"))

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIChatCompletionsAdapter().stream(request, "fake-key")
    ]

    assert events == [
        {
            "type": "reasoning_summary_unavailable",
            "provider": "openai",
            "reason": "unsupported_provider",
        },
        {
            "type": "final",
            "usage": {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0},
        },
    ]


@pytest.mark.anyio
async def test_chat_completions_adapter_rejects_native_tools_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client_creation(**_kwargs):
        raise AssertionError("provider client must not be created")

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        fail_client_creation,
    )
    request = _build_request(native_tools=[NativeToolSpec(id="web_search")])

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIChatCompletionsAdapter().stream(request, "fake-key")
    ]

    assert events == [
        {
            "type": "error",
            "code": "OPENAI_NATIVE_TOOLS_UNSUPPORTED",
            "message": "OpenAI native tools require the Responses API surface",
            "retryable": False,
        }
    ]


@pytest.mark.anyio
async def test_chat_completions_adapter_retries_transient_failure_before_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def stream_response(request_attempt: int):
        if request_attempt == 1:
            raise httpx.ConnectError("connection interrupted")
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    class FakeCompletions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            return stream_response(calls)

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.settings."
        "OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        100,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.settings."
        "PROVIDER_RETRY_BACKOFF_MS",
        1,
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIChatCompletionsAdapter().stream(
            _build_request(),
            "fake-key",
        )
    ]

    assert calls == 2
    assert events == [
        {
            "type": "final",
            "usage": {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0},
        }
    ]


@pytest.mark.anyio
async def test_chat_completions_adapter_does_not_retry_after_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def stream_response():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    index=0,
                    delta=SimpleNamespace(content="partial"),
                )
            ],
            usage=None,
        )
        raise httpx.ConnectError("connection interrupted")

    class FakeCompletions:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )
    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.settings."
        "OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        100,
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIChatCompletionsAdapter().stream(
            _build_request(),
            "fake-key",
        )
    ]

    assert calls == 1
    assert events[0] == {"type": "delta", "text": "partial"}
    assert events[1]["type"] == "error"
    assert events[1]["code"] == "PROVIDER_UNAVAILABLE"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_call", "expected_code", "expected_message"),
    [
        (
            {
                "index": 0,
                "id": "call_invalid",
                "function": {
                    "name": "lookup_pod",
                    "arguments": "{\"id\":oops",
                },
            },
            "OPENAI_TOOL_ARGUMENTS_INVALID",
            "Provider returned invalid tool arguments",
        ),
        (
            {
                "index": 0,
                "id": "call_invalid",
                "function": {"name": "lookup_pod", "arguments": "[]"},
            },
            "OPENAI_TOOL_ARGUMENTS_INVALID",
            "Provider returned invalid tool arguments",
        ),
        (
            {
                "index": 0,
                "function": {"name": "lookup_pod", "arguments": "{}"},
            },
            "OPENAI_TOOL_CALL_INVALID",
            "Provider returned an invalid tool call",
        ),
        (
            {
                "index": 0,
                "id": "call_invalid",
                "function": {"arguments": "{}"},
            },
            "OPENAI_TOOL_CALL_INVALID",
            "Provider returned an invalid tool call",
        ),
    ],
    ids=["malformed-arguments", "non-object-arguments", "missing-id", "missing-name"],
)
async def test_chat_completions_adapter_rejects_invalid_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    tool_call: dict,
    expected_code: str,
    expected_message: str,
) -> None:
    async def stream_response():
        yield _to_namespace(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [tool_call]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": None,
            }
        )

    class FakeCompletions:
        async def create(self, **_kwargs):
            return stream_response()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(
        "app.llm.adapters.openai_chat_completions_adapter.AsyncOpenAI",
        FakeClient,
    )

    events = [
        event.model_dump(exclude_none=True)
        async for event in OpenAIChatCompletionsAdapter().stream(
            _build_request(),
            "fake-key",
        )
    ]

    assert events == [
        {
            "type": "error",
            "code": expected_code,
            "message": expected_message,
            "retryable": False,
        }
    ]
