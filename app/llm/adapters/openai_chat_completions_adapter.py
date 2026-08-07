"""OpenAI Chat Completions adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import structlog
from openai import AsyncOpenAI, BadRequestError

from app.config.settings import settings
from app.llm.adapters.common import (
    build_openai_chat_completion_tools,
    parse_openai_tool_arguments,
    should_retry_openai_with_max_tokens,
    should_retry_openai_without_reasoning,
    should_retry_openai_without_stream_options,
    should_retry_openai_without_temperature,
    supports_openai_custom_temperature,
)
from app.llm.adapters.openai_tool_diagnostics import (
    observe_openai_tool_arguments,
    openai_tool_error_event,
)
from app.llm.adapters.provider_errors import provider_failure_event
from app.llm.provider_diagnostics import log_provider_stream_failure, provider_base_url
from app.llm.renderers import render_openai_chat_messages
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
MAX_TOOL_ARGUMENT_BYTES = 4_194_304


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


def _optional_usage_payload(
    usage: Any,
    tool_calls_count: int = 0,
) -> dict[str, int] | None:
    return _usage_payload(usage, tool_calls_count) if usage is not None else None


def _serialized_argument_bytes(arguments: str) -> int:
    try:
        return len(arguments.encode("utf-8"))
    except UnicodeEncodeError:
        return MAX_TOOL_ARGUMENT_BYTES + 1


def _merge_name_fragment(current: str, incoming: str) -> str:
    if not current:
        return incoming
    if incoming == current or current.startswith(incoming):
        return current
    if incoming.startswith(current):
        return incoming
    return current + incoming


def _merge_argument_fragment(current: str, incoming: str) -> tuple[str, bool]:
    if not current:
        return incoming, False
    if incoming == current:
        return current, True
    if len(incoming) > len(current) and incoming.startswith(current):
        return incoming, True
    return current + incoming, False


def _accumulate_tool_call(
    tool_calls: dict[int, dict[str, Any]],
    fragment: Any,
) -> str | None:
    raw_index = getattr(fragment, "index", None)
    call_id = str(getattr(fragment, "id", "") or "")
    if raw_index is None:
        matching = [
            index
            for index, current in tool_calls.items()
            if call_id and current["id"] == call_id
        ]
        if len(matching) == 1:
            index = matching[0]
        elif not tool_calls:
            index = 0
        elif len(tool_calls) == 1 and not call_id:
            index = next(iter(tool_calls))
        else:
            return "ambiguous tool call index"
    else:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return "invalid tool call index"

    current = tool_calls.setdefault(
        index,
        {
            "id": "",
            "name": "",
            "arguments": "",
            "fragment_count": 0,
            "outcome": "incremental",
            "invalid_arguments": False,
        },
    )
    if call_id:
        if current["id"] and current["id"] != call_id:
            return "conflicting tool call id"
        current["id"] = call_id
    function = getattr(fragment, "function", None)
    name = str(getattr(function, "name", "") or "")
    if name:
        current["name"] = _merge_name_fragment(current["name"], name)

    raw_arguments = getattr(function, "arguments", None)
    if isinstance(raw_arguments, Mapping):
        try:
            arguments = json.dumps(
                dict(raw_arguments),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            current["invalid_arguments"] = True
        else:
            if (
                _serialized_argument_bytes(arguments) > MAX_TOOL_ARGUMENT_BYTES
                or current["arguments"]
                and current["arguments"] != arguments
            ):
                current["invalid_arguments"] = True
            else:
                current["arguments"] = arguments
            current["outcome"] = "object_normalized"
    elif isinstance(raw_arguments, str) and raw_arguments:
        merged_arguments, cumulative = _merge_argument_fragment(
            current["arguments"],
            raw_arguments,
        )
        if _serialized_argument_bytes(merged_arguments) > MAX_TOOL_ARGUMENT_BYTES:
            current["invalid_arguments"] = True
        else:
            current["arguments"] = merged_arguments
        if cumulative and not current["invalid_arguments"]:
            current["outcome"] = "cumulative_normalized"
    elif raw_arguments not in (None, ""):
        current["invalid_arguments"] = True

    current["fragment_count"] += 1
    return None


def _tool_call_events(
    tool_calls: dict[int, dict[str, Any]],
    *,
    req: NormalizedLLMRequest,
    usage: Any,
    accumulation_error: str | None,
) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    if accumulation_error:
        safe_call = next(iter(tool_calls.values())) if len(tool_calls) == 1 else None
        safe_tool = str(safe_call["name"] or "") if safe_call else ""
        safe_call_id = (
            str(safe_call["id"] or "")
            if safe_call and accumulation_error != "conflicting tool call id"
            else ""
        )
        argument_bytes = (
            _serialized_argument_bytes(str(safe_call["arguments"]))
            if safe_call
            else 0
        )
        return [
            openai_tool_error_event(
                req,
                api_surface="chat_completions",
                code="OPENAI_TOOL_CALL_INVALID",
                reason=accumulation_error,
                call_id=safe_call_id,
                tool=safe_tool or None,
                argument_bytes=argument_bytes,
                fragment_count=safe_call["fragment_count"] if safe_call else 0,
                usage=_optional_usage_payload(usage),
            )
        ]
    for index in sorted(tool_calls):
        tool_call = tool_calls[index]
        argument_bytes = _serialized_argument_bytes(tool_call["arguments"])
        if not tool_call["id"] or not tool_call["name"]:
            return [
                openai_tool_error_event(
                    req,
                    api_surface="chat_completions",
                    code="OPENAI_TOOL_CALL_INVALID",
                    reason="missing call identity",
                    call_id=tool_call["id"] or None,
                    tool=tool_call["name"] or None,
                    argument_bytes=argument_bytes,
                    fragment_count=tool_call["fragment_count"],
                    usage=_optional_usage_payload(usage),
                )
            ]
        arguments = (
            None
            if tool_call["invalid_arguments"]
            else parse_openai_tool_arguments(tool_call["arguments"])
        )
        if arguments is None:
            return [
                openai_tool_error_event(
                    req,
                    api_surface="chat_completions",
                    code="OPENAI_TOOL_ARGUMENTS_INVALID",
                    reason="malformed or non-object arguments",
                    call_id=tool_call["id"],
                    tool=tool_call["name"],
                    argument_bytes=argument_bytes,
                    fragment_count=tool_call["fragment_count"],
                    usage=_optional_usage_payload(usage),
                )
            ]
        observe_openai_tool_arguments(
            "chat_completions", tool_call["outcome"], argument_bytes
        )
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
        include_stream_options = True
        use_max_completion_tokens = True
        summary_requested = reasoning_summaries_enabled(req)

        def build_request_kwargs(
            include_temp: bool,
            include_reasoning_effort: bool,
            include_stream_options: bool,
            use_max_completion_tokens: bool,
        ) -> dict[str, Any]:
            request_kwargs: dict[str, Any] = {
                "model": req.model,
                "messages": render_openai_chat_messages(req),
                "stream": True,
            }
            if include_stream_options:
                request_kwargs["stream_options"] = {"include_usage": True}
            if req.max_output_tokens is not None:
                token_parameter = (
                    "max_completion_tokens" if use_max_completion_tokens else "max_tokens"
                )
                request_kwargs[token_parameter] = req.max_output_tokens
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
            tool_calls: dict[int, dict[str, Any]] = {}
            accumulation_error: str | None = None
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
                current_include_stream_options = include_stream_options
                current_use_max_completion_tokens = use_max_completion_tokens
                while True:
                    request_kwargs = build_request_kwargs(
                        current_include_temperature,
                        current_include_reasoning,
                        current_include_stream_options,
                        current_use_max_completion_tokens,
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
                            include_temperature = False
                            continue
                        if should_retry_openai_without_reasoning(
                            error_message,
                            current_include_reasoning,
                        ):
                            current_include_reasoning = False
                            include_reasoning = False
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
                        if should_retry_openai_without_stream_options(
                            error_message,
                            current_include_stream_options,
                        ):
                            current_include_stream_options = False
                            include_stream_options = False
                            continue
                        if should_retry_openai_with_max_tokens(
                            error_message,
                            current_use_max_completion_tokens,
                        ):
                            current_use_max_completion_tokens = False
                            use_max_completion_tokens = False
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
                            if accumulation_error is None:
                                accumulation_error = _accumulate_tool_call(
                                    tool_calls,
                                    fragment,
                                )

                tool_call_events = _tool_call_events(
                    tool_calls,
                    req=req,
                    usage=usage,
                    accumulation_error=accumulation_error,
                )
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
