import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any

import structlog
from google import genai
from google.genai import types

from app.config.settings import settings
from app.llm.adapters.common import build_gemini_tools
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


async def _close_client(client: genai.Client) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _extract_function_calls(chunk: Any) -> list[Any]:
    function_calls = getattr(chunk, "function_calls", None)
    if function_calls:
        return list(function_calls)

    calls: list[Any] = []
    for candidate in getattr(chunk, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            function_call = getattr(part, "function_call", None)
            if function_call:
                calls.append(function_call)
    return calls


def _function_call_args(function_call: Any) -> dict[str, Any]:
    args = getattr(function_call, "args", None)
    return dict(args or {})


class GeminiAdapter(LLMAdapter):
    """
    Adapter for Google's Gemini API via google-genai.
    Handles streaming responses, function calls, and usage metadata.
    """

    async def stream(self, req: NormalizedLLMRequest, api_key: str) -> AsyncIterator[StreamEvent]:
        """
        Streams a response from Gemini and translates events to the gateway format.
        """
        client = genai.Client(api_key=api_key)
        tool_calls_count = 0
        tool_call_seq = 0

        # Convert messages to Gemini format
        contents = []
        for m in req.messages:
            role = "user" if m.role == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m.content}]})

        generation_config_kwargs = {"temperature": req.temperature}
        if req.max_output_tokens is not None:
            generation_config_kwargs["max_output_tokens"] = req.max_output_tokens
        if req.tools:
            generation_config_kwargs["tools"] = build_gemini_tools(req.tools)
            generation_config_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            )

        request_config = types.GenerateContentConfig(**generation_config_kwargs)

        dependency_key = "provider:gemini"
        attempts = max(1, settings.PROVIDER_RETRY_ATTEMPTS)
        attempt = 1

        try:
            while attempt <= attempts:
                tool_calls_count = 0
                tool_call_seq = 0
                emitted_event = False
                try:
                    await dependency_circuit_breaker.before_call(
                        dependency_key, "provider", "gemini"
                    )
                    response = await client.aio.models.generate_content_stream(
                        model=req.model,
                        contents=contents,
                        config=request_config,
                    )

                    usage = None
                    async for chunk in response:
                        usage = getattr(chunk, "usage_metadata", None) or usage

                        try:
                            if chunk.text:
                                emitted_event = True
                                yield StreamEvent(type="delta", text=chunk.text)
                        except ValueError:
                            # The SDK raises when a chunk contains only function calls.
                            pass

                        for fn in _extract_function_calls(chunk):
                            tool_calls_count += 1
                            tool_call_seq += 1
                            emitted_event = True
                            yield StreamEvent(
                                type="tool_call",
                                call_id=f"gemini_call_{tool_call_seq}",
                                tool=fn.name,
                                arguments=_function_call_args(fn),
                            )

                    emitted_event = True
                    yield StreamEvent(
                        type="final",
                        usage={
                            "input_tokens": usage.prompt_token_count if usage else 0,
                            "output_tokens": usage.candidates_token_count if usage else 0,
                            "tool_calls": tool_calls_count,
                        },
                    )
                    await dependency_circuit_breaker.record_success(dependency_key)
                    return
                except CircuitOpenError as exc:
                    logger.warning("provider_circuit_open", provider="gemini", error=str(exc))
                    yield StreamEvent(
                        type="error",
                        code="GEMINI_ERROR",
                        message=PROVIDER_TEMPORARILY_UNAVAILABLE,
                        retryable=True,
                    )
                    return
                except Exception as exc:
                    note_dependency_event("provider", "failure")
                    retryable = is_retryable_dependency_error(exc)
                    logger.warning(
                        "provider_stream_failed",
                        provider="gemini",
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
                        code="GEMINI_ERROR",
                        message=PROVIDER_REQUEST_FAILED,
                        retryable=retryable,
                    )
                    return
        finally:
            await _close_client(client)
