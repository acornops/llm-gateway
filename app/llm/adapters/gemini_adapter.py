import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any

import structlog
from google import genai
from google.genai import types

from app.config.settings import settings
from app.llm.adapters.common import build_gemini_tools
from app.llm.service import (
    LLMAdapter,
    NormalizedLLMRequest,
    StreamEvent,
    model_reasoning_enabled,
    reasoning_summaries_enabled,
)
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


def _extract_text_parts(chunk: Any) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    for candidate in getattr(chunk, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                parts.append((str(text), bool(getattr(part, "thought", False))))
    return parts


def _thinking_config_kwargs(model: str, effort: str, include_thoughts: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"include_thoughts": include_thoughts}
    normalized = model.lower()
    if "gemini-3" in normalized and effort != "off":
        kwargs["thinking_level"] = {
            "low": "LOW",
            "medium": "MEDIUM",
            "high": "HIGH",
        }.get(effort, "MEDIUM")
    elif "gemini-2.5" in normalized and effort != "off":
        kwargs["thinking_budget"] = {
            "low": 1024,
            "medium": 4096,
            "high": 8192,
        }.get(effort, 4096)
    return kwargs


class GeminiAdapter(LLMAdapter):
    """
    Adapter for Google's Gemini API via google-genai.
    Handles streaming responses, function calls, and usage metadata.
    """

    async def stream(self, req: NormalizedLLMRequest, api_key: str) -> AsyncIterator[StreamEvent]:
        """
        Streams a response from Gemini and translates events to the gateway format.
        """
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if settings.LLM_PROVIDER_GEMINI_BASE_URL:
            client_kwargs["http_options"] = types.HttpOptions(
                base_url=settings.LLM_PROVIDER_GEMINI_BASE_URL
            )
        client = genai.Client(**client_kwargs)
        tool_calls_count = 0
        tool_call_seq = 0
        summary_requested = reasoning_summaries_enabled(req)

        system_instruction = "\n\n".join(
            m.content for m in req.messages if m.role == "system"
        ).strip()

        # Convert messages to Gemini format
        contents = []
        for m in req.messages:
            if m.role == "system":
                continue
            role = "user" if m.role == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m.content}]})

        generation_config_kwargs = {"temperature": req.temperature}
        if system_instruction:
            generation_config_kwargs["system_instruction"] = system_instruction
        if req.max_output_tokens is not None:
            generation_config_kwargs["max_output_tokens"] = req.max_output_tokens
        gemini_tools: list[Any] = []
        if req.tools:
            gemini_tools.extend(build_gemini_tools(req.tools))
        if any(tool.id == "web_search" for tool in req.native_tools):
            gemini_tools.append(types.Tool(google_search=types.GoogleSearch()))
        if gemini_tools:
            generation_config_kwargs["tools"] = gemini_tools
        if req.tools:
            generation_config_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            )
        if model_reasoning_enabled(req):
            generation_config_kwargs["thinking_config"] = types.ThinkingConfig(
                **_thinking_config_kwargs(req.model, req.reasoning.effort, summary_requested)
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
                summary_text = ""
                summary_seen = False
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

                        text_parts = _extract_text_parts(chunk)
                        if text_parts:
                            for text, is_thought in text_parts:
                                if is_thought and summary_requested:
                                    emitted_event = True
                                    summary_seen = True
                                    summary_text += text
                                    yield StreamEvent(
                                        type="reasoning_summary_delta",
                                        text=text,
                                        provider="gemini",
                                    )
                                elif not is_thought:
                                    emitted_event = True
                                    yield StreamEvent(type="delta", text=text)
                        else:
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

                    if summary_requested:
                        if summary_seen and summary_text:
                            emitted_event = True
                            yield StreamEvent(
                                type="reasoning_summary_completed",
                                text=summary_text,
                                provider="gemini",
                            )
                        elif not summary_seen:
                            emitted_event = True
                            logger.info(
                                "provider_reasoning_summary_degraded",
                                provider="gemini",
                                model=req.model,
                                run_id=req.run_id,
                                workspace_id=req.workspace_id,
                                reason="provider_omitted",
                            )
                            yield StreamEvent(
                                type="reasoning_summary_unavailable",
                                provider="gemini",
                                reason="provider_omitted",
                            )

                    reasoning_tokens = (
                        int(getattr(usage, "thoughts_token_count", 0) or 0)
                        if usage
                        else 0
                    )
                    usage_payload = {
                        "input_tokens": usage.prompt_token_count if usage else 0,
                        "output_tokens": usage.candidates_token_count if usage else 0,
                        "tool_calls": tool_calls_count,
                    }
                    if reasoning_tokens:
                        usage_payload["reasoning_tokens"] = reasoning_tokens
                    emitted_event = True
                    yield StreamEvent(
                        type="final",
                        usage=usage_payload,
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
