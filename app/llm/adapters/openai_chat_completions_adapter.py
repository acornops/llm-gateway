"""OpenAI Chat Completions adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import structlog
from openai import AsyncOpenAI, BadRequestError

from app.config.settings import settings
from app.llm.adapters.common import (
    build_openai_chat_completion_tools,
    parse_openai_tool_arguments,
    should_retry_openai_without_reasoning,
    should_retry_openai_without_temperature,
    supports_openai_custom_temperature,
)
from app.llm.adapters.provider_errors import provider_failure_event
from app.llm.provider_diagnostics import log_provider_stream_failure, provider_base_url
from app.llm.service import (
    LLMAdapter,
    NormalizedLLMRequest,
    StreamEvent,
    reasoning_summaries_enabled,
)
from app.outbound_tls import provider_http_client
from app.resilience.outbound import (
    CircuitOpenError,
    backoff_seconds,
    dependency_circuit_breaker,
    is_retryable_dependency_error,
    note_dependency_event,
)

logger = structlog.get_logger()

PROVIDER_TEMPORARILY_UNAVAILABLE = "Provider temporarily unavailable"
NATIVE_TOOLS_UNSUPPORTED = "OpenAI native tools require the Responses API surface"


def _invalid_tool_call_event(
    code: str,
    message: str,
) -> StreamEvent:
    return StreamEvent(
        type="error",
        code=code,
        message=message,
        retryable=False,
    )


def _client(api_key: str) -> AsyncOpenAI:
    client_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": provider_base_url("openai"),
    }
    http_client = provider_http_client("openai")
    if http_client is not None:
        client_kwargs["http_client"] = http_client
    return AsyncOpenAI(**client_kwargs)


def _usage_payload(usage: Any, tool_calls_count: int) -> dict[str, int]:
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
    payload = {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "tool_calls": tool_calls_count,
    }
    if reasoning_tokens:
        payload["reasoning_tokens"] = reasoning_tokens
    return payload


def _accumulate_tool_call(
    tool_calls: dict[int, dict[str, str]],
    fragment: Any,
) -> None:
    index = int(getattr(fragment, "index", 0) or 0)
    current = tool_calls.setdefault(
        index,
        {"id": "", "name": "", "arguments": ""},
    )
    call_id = str(getattr(fragment, "id", "") or "")
    if call_id:
        current["id"] = call_id
    function = getattr(fragment, "function", None)
    name = str(getattr(function, "name", "") or "")
    arguments = str(getattr(function, "arguments", "") or "")
    if name:
        current["name"] += name
    if arguments:
        current["arguments"] += arguments


def _tool_call_events(
    tool_calls: dict[int, dict[str, str]],
) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for index in sorted(tool_calls):
        tool_call = tool_calls[index]
        if not tool_call["id"] or not tool_call["name"]:
            return [
                _invalid_tool_call_event(
                    "OPENAI_TOOL_CALL_INVALID",
                    "Provider returned an invalid tool call",
                )
            ]
        arguments = parse_openai_tool_arguments(tool_call["arguments"])
        if arguments is None:
            return [
                _invalid_tool_call_event(
                    "OPENAI_TOOL_ARGUMENTS_INVALID",
                    "Provider returned invalid tool arguments",
                )
            ]
        events.append(
            StreamEvent(
                type="tool_call",
                call_id=tool_call["id"],
                tool=tool_call["name"],
                arguments=arguments,
            )
        )
    return events


class OpenAIChatCompletionsAdapter(LLMAdapter):
    """Streams OpenAI Chat Completions into the normalized gateway contract."""

    async def stream(
        self,
        req: NormalizedLLMRequest,
        api_key: str,
    ) -> AsyncIterator[StreamEvent]:
        if req.native_tools:
            yield StreamEvent(
                type="error",
                code="OPENAI_NATIVE_TOOLS_UNSUPPORTED",
                message=NATIVE_TOOLS_UNSUPPORTED,
                retryable=False,
            )
            return

        client = _client(api_key)
        openai_tools = build_openai_chat_completion_tools(req.tools)
        include_temperature = supports_openai_custom_temperature(req.model)
        include_reasoning = req.reasoning.effort != "off"
        summary_requested = reasoning_summaries_enabled(req)

        def build_request_kwargs(
            include_temp: bool,
            include_reasoning_effort: bool,
        ) -> dict[str, Any]:
            request_kwargs: dict[str, Any] = {
                "model": req.model,
                "messages": [message.model_dump() for message in req.messages],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if req.max_output_tokens is not None:
                request_kwargs["max_completion_tokens"] = req.max_output_tokens
            if include_temp:
                request_kwargs["temperature"] = req.temperature
            if openai_tools:
                request_kwargs["tools"] = openai_tools
                request_kwargs["tool_choice"] = "auto"
            if include_reasoning_effort:
                request_kwargs["reasoning_effort"] = req.reasoning.effort
            return request_kwargs

        dependency_key = "provider:openai"
        attempts = max(1, settings.PROVIDER_RETRY_ATTEMPTS)
        attempt = 1

        while attempt <= attempts:
            tool_calls: dict[int, dict[str, str]] = {}
            usage: Any = None
            emitted_event = False
            try:
                await dependency_circuit_breaker.before_call(
                    dependency_key,
                    "provider",
                    "openai",
                )

                current_include_temperature = include_temperature
                current_include_reasoning = include_reasoning
                while True:
                    request_kwargs = build_request_kwargs(
                        current_include_temperature,
                        current_include_reasoning,
                    )
                    try:
                        stream = await client.chat.completions.create(**request_kwargs)
                        break
                    except BadRequestError as error:
                        error_message = str(error)
                        if should_retry_openai_without_temperature(
                            error_message,
                            current_include_temperature,
                        ):
                            current_include_temperature = False
                            continue
                        if should_retry_openai_without_reasoning(
                            error_message,
                            current_include_reasoning,
                        ):
                            current_include_reasoning = False
                            logger.info(
                                "provider_reasoning_effort_degraded",
                                provider="openai",
                                api_surface="chat_completions",
                                model=req.model,
                                run_id=req.run_id,
                                workspace_id=req.workspace_id,
                                reason="unsupported_model",
                            )
                            continue
                        raise

                async for chunk in stream:
                    usage = getattr(chunk, "usage", None) or usage
                    for choice in getattr(chunk, "choices", None) or []:
                        if int(getattr(choice, "index", 0) or 0) != 0:
                            continue
                        delta = getattr(choice, "delta", None)
                        text = str(getattr(delta, "content", "") or "")
                        if text:
                            emitted_event = True
                            yield StreamEvent(type="delta", text=text)
                        for fragment in getattr(delta, "tool_calls", None) or []:
                            _accumulate_tool_call(tool_calls, fragment)

                tool_call_events = _tool_call_events(tool_calls)
                if any(event.type == "error" for event in tool_call_events):
                    await dependency_circuit_breaker.record_success(dependency_key)
                    emitted_event = True
                    yield tool_call_events[0]
                    return
                for event in tool_call_events:
                    emitted_event = True
                    yield event

                if summary_requested:
                    logger.info(
                        "provider_reasoning_summary_degraded",
                        provider="openai",
                        api_surface="chat_completions",
                        model=req.model,
                        run_id=req.run_id,
                        workspace_id=req.workspace_id,
                        reason="unsupported_provider",
                    )
                    emitted_event = True
                    yield StreamEvent(
                        type="reasoning_summary_unavailable",
                        provider="openai",
                        reason="unsupported_provider",
                    )

                emitted_event = True
                yield StreamEvent(
                    type="final",
                    usage=_usage_payload(usage, len(tool_call_events)),
                )
                await dependency_circuit_breaker.record_success(dependency_key)
                return
            except CircuitOpenError as exc:
                logger.warning(
                    "provider_circuit_open",
                    provider="openai",
                    api_surface="chat_completions",
                    error=str(exc),
                )
                yield StreamEvent(
                    type="error",
                    code="OPENAI_ERROR",
                    message=PROVIDER_TEMPORARILY_UNAVAILABLE,
                    retryable=True,
                )
                return
            except Exception as exc:
                note_dependency_event("provider", "failure")
                retryable = is_retryable_dependency_error(exc)
                log_provider_stream_failure(
                    logger,
                    provider="openai",
                    model=req.model,
                    run_id=req.run_id,
                    workspace_id=req.workspace_id,
                    attempt=attempt,
                    max_attempts=attempts,
                    emitted_event=emitted_event,
                    retryable=retryable,
                    exc=exc,
                )
                if retryable:
                    opened = await dependency_circuit_breaker.record_failure(
                        dependency_key,
                        settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                        settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
                    )
                    if opened:
                        note_dependency_event("provider", "circuit_open")
                    if attempt < attempts and not emitted_event and not opened:
                        note_dependency_event("provider", "retry")
                        await asyncio.sleep(
                            backoff_seconds(
                                settings.PROVIDER_RETRY_BACKOFF_MS,
                                attempt,
                            )
                        )
                        attempt += 1
                        continue
                yield provider_failure_event(
                    exc,
                    fallback_code="OPENAI_ERROR",
                    retryable=retryable,
                )
                return
