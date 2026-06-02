import asyncio
import json
from collections.abc import AsyncIterator

import structlog
from openai import AsyncOpenAI, BadRequestError

from app.config.settings import settings
from app.llm.adapters.common import (
    alternate_openai_output_token_param,
    build_openai_tools,
    resolve_openai_output_token_param,
    should_retry_openai_with_alt_token_param,
    should_retry_openai_without_temperature,
    supports_openai_custom_temperature,
)
from app.llm.service import LLMAdapter, NormalizedLLMRequest, StreamEvent
from app.resilience.outbound import (
    CircuitOpenError,
    backoff_seconds,
    dependency_circuit_breaker,
    is_retryable_dependency_error,
    note_dependency_event,
)

logger = structlog.get_logger()

PROVIDER_TEMPORARILY_UNAVAILABLE = "Provider temporarily unavailable"
PROVIDER_REQUEST_FAILED = "Provider request failed"


class OpenAIAdapter(LLMAdapter):
    """
    Adapter for OpenAI's Chat Completions API.
    Handles streaming responses, tool call accumulation, and usage reporting.
    """

    async def stream(self, req: NormalizedLLMRequest, api_key: str) -> AsyncIterator[StreamEvent]:
        """
        Streams a chat completion from OpenAI and translates events to the gateway format.
        """
        client = AsyncOpenAI(api_key=api_key)

        # In-memory accumulation of tool call arguments
        tool_calls_map = {}
        openai_tools = build_openai_tools(req.tools)

        token_param = resolve_openai_output_token_param(req.model)
        include_temperature = supports_openai_custom_temperature(req.model)

        def build_request_kwargs(output_token_param: str, include_temp: bool) -> dict:
            request_kwargs = {
                "model": req.model,
                "messages": [m.model_dump() for m in req.messages],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if req.max_output_tokens is not None:
                request_kwargs[output_token_param] = req.max_output_tokens
            if include_temp:
                request_kwargs["temperature"] = req.temperature
            if openai_tools:
                request_kwargs["tools"] = openai_tools
                request_kwargs["tool_choice"] = "auto"
            return request_kwargs

        dependency_key = "provider:openai"
        attempts = max(1, settings.PROVIDER_RETRY_ATTEMPTS)
        attempt = 1

        while attempt <= attempts:
            tool_calls_map = {}
            emitted_event = False
            try:
                await dependency_circuit_breaker.before_call(dependency_key, "provider", "openai")

                stream = None
                compatibility_attempts_remaining = 3
                current_token_param = token_param
                current_include_temperature = include_temperature
                while stream is None and compatibility_attempts_remaining > 0:
                    request_kwargs = build_request_kwargs(
                        current_token_param,
                        current_include_temperature,
                    )
                    try:
                        stream = await client.chat.completions.create(**request_kwargs)
                    except BadRequestError as error:
                        compatibility_attempts_remaining -= 1
                        error_message = str(error)
                        if should_retry_openai_with_alt_token_param(
                            error_message,
                            current_token_param,
                        ):
                            current_token_param = alternate_openai_output_token_param(
                                current_token_param
                            )
                            continue
                        if should_retry_openai_without_temperature(
                            error_message,
                            current_include_temperature,
                        ):
                            current_include_temperature = False
                            continue
                        raise
                if stream is None:
                    raise RuntimeError("OpenAI request failed after compatibility retries.")

                async for chunk in stream:
                    # Handle usage (often in the last chunk with stream_options)
                    if chunk.usage:
                        emitted_event = True
                        yield StreamEvent(
                            type="final",
                            usage={
                                "input_tokens": chunk.usage.prompt_tokens,
                                "output_tokens": chunk.usage.completion_tokens,
                                "tool_calls": len(tool_calls_map),
                            },
                        )
                        continue

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    if delta.content:
                        emitted_event = True
                        yield StreamEvent(type="delta", text=delta.content)

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            if tc.id:  # Start of a new tool call
                                tool_calls_map[tc.index] = {
                                    "id": tc.id,
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "",
                                }
                            else:  # Continuation of an existing tool call
                                if tc.index in tool_calls_map:
                                    tool_calls_map[tc.index]["arguments"] += (
                                        tc.function.arguments or ""
                                    )

                    # If finish_reason is tool_calls, emit all accumulated tool calls
                    if chunk.choices[0].finish_reason == "tool_calls":
                        for tc in tool_calls_map.values():
                            # Only yield if we haven't yielded this one yet
                            if not tc.get("yielded"):
                                arguments = {}
                                if tc["arguments"]:
                                    try:
                                        arguments = json.loads(tc["arguments"])
                                    except json.JSONDecodeError:
                                        arguments = {}
                                emitted_event = True
                                yield StreamEvent(
                                    type="tool_call",
                                    call_id=tc["id"],
                                    tool=tc["name"],
                                    arguments=arguments,
                                )
                                tc["yielded"] = True

                await dependency_circuit_breaker.record_success(dependency_key)
                return
            except CircuitOpenError as exc:
                logger.warning("provider_circuit_open", provider="openai", error=str(exc))
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
                logger.warning(
                    "provider_stream_failed",
                    provider="openai",
                    attempt=attempt,
                    max_attempts=attempts,
                    emitted_event=emitted_event,
                    retryable=retryable,
                    error=str(exc),
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
                            backoff_seconds(settings.PROVIDER_RETRY_BACKOFF_MS, attempt)
                        )
                        attempt += 1
                        continue
                yield StreamEvent(
                    type="error",
                    code="OPENAI_ERROR",
                    message=PROVIDER_REQUEST_FAILED,
                    retryable=retryable,
                )
                return
