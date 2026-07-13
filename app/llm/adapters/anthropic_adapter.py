import asyncio
import json
from collections.abc import AsyncIterator

import structlog
from anthropic import AsyncAnthropic

from app.config.settings import settings
from app.llm.adapters.common import build_anthropic_tools
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


def _thinking_budget(max_tokens: int, effort: str) -> int:
    requested = {
        "off": 1024,
        "low": 1024,
        "medium": 4096,
        "high": 8192,
    }.get(effort, 2048)
    return max(1, min(requested, max_tokens - 1))


def _can_request_thinking(max_tokens: int) -> bool:
    return max_tokens > 1


def _uses_adaptive_thinking(model: str) -> bool:
    normalized = model.lower()
    return (
        "fable-5" in normalized
        or "opus-4-8" in normalized
        or "opus-4.8" in normalized
        or "4-6" in normalized
        or "4.6" in normalized
    )


class AnthropicAdapter(LLMAdapter):
    """
    Adapter for Anthropic's Messages API.
    Handles streaming responses, tool calling, and usage reporting.
    """
    DEFAULT_MAX_TOKENS = 8192

    async def stream(self, req: NormalizedLLMRequest, api_key: str) -> AsyncIterator[StreamEvent]:
        """
        Streams a message from Anthropic and translates events to the gateway format.
        """
        client_kwargs = {"api_key": api_key}
        if settings.LLM_PROVIDER_ANTHROPIC_BASE_URL:
            client_kwargs["base_url"] = settings.LLM_PROVIDER_ANTHROPIC_BASE_URL
        client = AsyncAnthropic(**client_kwargs)
        tool_calls_map = {}
        anthropic_tools = build_anthropic_tools(req.tools, req.native_tools)
        max_tokens = req.max_output_tokens or self.DEFAULT_MAX_TOKENS
        summary_requested = reasoning_summaries_enabled(req)

        stream_kwargs = {
            "model": req.model,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in req.messages
                if m.role != "system"
            ],
            "system": next((m.content for m in req.messages if m.role == "system"), None),
            # Anthropic requires max_tokens in request payload.
            "max_tokens": max_tokens,
            "temperature": req.temperature,
        }
        if anthropic_tools:
            stream_kwargs["tools"] = anthropic_tools
        if model_reasoning_enabled(req) and _can_request_thinking(max_tokens):
            if _uses_adaptive_thinking(req.model):
                thinking: dict[str, object] = {
                    "type": "adaptive",
                }
                if summary_requested:
                    thinking["display"] = "summarized"
                if req.reasoning.effort != "off":
                    thinking["effort"] = req.reasoning.effort
            else:
                thinking = {
                    "type": "enabled",
                    "budget_tokens": _thinking_budget(max_tokens, req.reasoning.effort),
                }
                if summary_requested:
                    thinking["display"] = "summarized"
            stream_kwargs["thinking"] = thinking

        dependency_key = "provider:anthropic"
        attempts = max(1, settings.PROVIDER_RETRY_ATTEMPTS)
        attempt = 1

        while attempt <= attempts:
            tool_calls_map = {}
            emitted_event = False
            thinking_text = ""
            thinking_summary_emitted = False
            unavailable_emitted = False
            try:
                await dependency_circuit_breaker.before_call(
                    dependency_key,
                    "provider",
                    "anthropic",
                )

                async with client.messages.stream(**stream_kwargs) as stream:
                    async for event in stream:
                        if event.type == "text":
                            emitted_event = True
                            yield StreamEvent(type="delta", text=event.text)
                        elif event.type == "content_block_start":
                            if event.content_block.type == "tool_use":
                                tool_calls_map[event.index] = {
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input": "",
                                }
                            elif event.content_block.type == "thinking":
                                block_text = str(getattr(event.content_block, "thinking", "") or "")
                                if block_text and summary_requested:
                                    thinking_text += block_text
                                    emitted_event = True
                                    yield StreamEvent(
                                        type="reasoning_summary_delta",
                                        text=block_text,
                                        provider="anthropic",
                                    )
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                emitted_event = True
                                yield StreamEvent(type="delta", text=event.delta.text)
                            elif event.delta.type == "thinking_delta":
                                delta_text = str(getattr(event.delta, "thinking", "") or "")
                                if delta_text and summary_requested:
                                    thinking_text += delta_text
                                    emitted_event = True
                                    yield StreamEvent(
                                        type="reasoning_summary_delta",
                                        text=delta_text,
                                        provider="anthropic",
                                    )
                            elif (
                                event.delta.type == "input_json_delta"
                                and event.index in tool_calls_map
                            ):
                                tool_calls_map[event.index]["input"] += event.delta.partial_json
                        elif event.type == "content_block_stop":
                            if event.index in tool_calls_map:
                                tc = tool_calls_map[event.index]
                                emitted_event = True
                                yield StreamEvent(
                                    type="tool_call",
                                    call_id=tc["id"],
                                    tool=tc["name"],
                                    arguments=json.loads(tc["input"]) if tc["input"] else {},
                                )
                            elif summary_requested and thinking_text:
                                emitted_event = True
                                yield StreamEvent(
                                    type="reasoning_summary_completed",
                                    text=thinking_text,
                                    provider="anthropic",
                                )
                                thinking_text = ""
                                thinking_summary_emitted = True
                        elif event.type == "message_start":
                            pass
                        elif event.type == "message_delta" and event.usage:
                            if (
                                summary_requested
                                and not thinking_summary_emitted
                                and not unavailable_emitted
                            ):
                                emitted_event = True
                                unavailable_emitted = True
                                logger.info(
                                    "provider_reasoning_summary_degraded",
                                    provider="anthropic",
                                    model=req.model,
                                    run_id=req.run_id,
                                    workspace_id=req.workspace_id,
                                    reason="provider_omitted",
                                )
                                yield StreamEvent(
                                    type="reasoning_summary_unavailable",
                                    provider="anthropic",
                                    reason="provider_omitted",
                                )
                            emitted_event = True
                            yield StreamEvent(
                                type="final",
                                usage={
                                    "input_tokens": event.usage.input_tokens,
                                    "output_tokens": event.usage.output_tokens,
                                    "tool_calls": len(tool_calls_map),
                                },
                            )

                await dependency_circuit_breaker.record_success(dependency_key)
                return
            except CircuitOpenError as exc:
                logger.warning("provider_circuit_open", provider="anthropic", error=str(exc))
                yield StreamEvent(
                    type="error",
                    code="ANTHROPIC_ERROR",
                    message=PROVIDER_TEMPORARILY_UNAVAILABLE,
                    retryable=True,
                )
                return
            except Exception as exc:
                note_dependency_event("provider", "failure")
                retryable = is_retryable_dependency_error(exc)
                logger.warning(
                    "provider_stream_failed",
                    provider="anthropic",
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
                    code="ANTHROPIC_ERROR",
                    message=PROVIDER_REQUEST_FAILED,
                    retryable=retryable,
                )
                return
