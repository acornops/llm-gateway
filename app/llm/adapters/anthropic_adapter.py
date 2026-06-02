import asyncio
import json
from collections.abc import AsyncIterator

import structlog
from anthropic import AsyncAnthropic

from app.config.settings import settings
from app.llm.adapters.common import build_anthropic_tools
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
        client = AsyncAnthropic(api_key=api_key)
        tool_calls_map = {}
        anthropic_tools = build_anthropic_tools(req.tools)

        stream_kwargs = {
            "model": req.model,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in req.messages
                if m.role != "system"
            ],
            "system": next((m.content for m in req.messages if m.role == "system"), None),
            # Anthropic requires max_tokens in request payload.
            "max_tokens": req.max_output_tokens or self.DEFAULT_MAX_TOKENS,
            "temperature": req.temperature,
        }
        if anthropic_tools:
            stream_kwargs["tools"] = anthropic_tools

        dependency_key = "provider:anthropic"
        attempts = max(1, settings.PROVIDER_RETRY_ATTEMPTS)
        attempt = 1

        while attempt <= attempts:
            tool_calls_map = {}
            emitted_event = False
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
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                emitted_event = True
                                yield StreamEvent(type="delta", text=event.delta.text)
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
                        elif event.type == "message_start":
                            pass
                        elif event.type == "message_delta" and event.usage:
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
