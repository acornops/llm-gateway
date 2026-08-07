import asyncio
from collections.abc import AsyncIterator

import structlog
from openai import AsyncOpenAI, BadRequestError

from app.config.settings import settings
from app.llm.adapters.common import (
    build_openai_response_tools,
    parse_openai_tool_arguments,
    should_retry_openai_without_reasoning,
    should_retry_openai_without_temperature,
    supports_openai_custom_temperature,
)
from app.llm.adapters.openai_chat_completions_adapter import (
    OpenAIChatCompletionsAdapter,
)
from app.llm.adapters.openai_tool_diagnostics import (
    observe_openai_tool_arguments,
    openai_tool_error_event,
)
from app.llm.adapters.provider_errors import provider_failure_event
from app.llm.openai_continuation import capture_openai_reasoning_input
from app.llm.provider_diagnostics import log_provider_stream_failure, provider_base_url
from app.llm.renderers import render_openai_responses_input
from app.llm.service import (
    LLMAdapter,
    NormalizedLLMRequest,
    StreamEvent,
    model_reasoning_enabled,
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


class OpenAIResponsesAdapter(LLMAdapter):
    """
    Adapter for OpenAI's Responses API.
    Handles streaming responses, tool call accumulation, and usage reporting.
    """

    async def stream(self, req: NormalizedLLMRequest, api_key: str) -> AsyncIterator[StreamEvent]:
        """
        Streams a generation from OpenAI and translates events to the gateway format.
        """
        client_kwargs = {
            "api_key": api_key,
            "base_url": provider_base_url("openai"),
        }
        http_client = provider_http_client("openai")
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        client = AsyncOpenAI(**client_kwargs)

        openai_tools = build_openai_response_tools(req.tools, req.native_tools)
        include_temperature = supports_openai_custom_temperature(req.model)
        summary_requested = reasoning_summaries_enabled(req)
        reasoning_requested = model_reasoning_enabled(req)

        def build_request_kwargs(include_temp: bool, include_reasoning: bool) -> dict:
            request_kwargs = {
                "model": req.model,
                "instructions": req.runtime_instruction,
                "input": render_openai_responses_input(req),
                "stream": True,
            }
            if req.max_output_tokens is not None:
                request_kwargs["max_output_tokens"] = req.max_output_tokens
            if include_temp:
                request_kwargs["temperature"] = req.temperature
            if openai_tools:
                request_kwargs["tools"] = openai_tools
                request_kwargs["tool_choice"] = "auto"
            if include_reasoning:
                reasoning = {}
                if summary_requested:
                    reasoning["summary"] = req.reasoning.summary_mode
                if req.reasoning.effort != "off":
                    reasoning["effort"] = req.reasoning.effort
                if reasoning:
                    request_kwargs["reasoning"] = reasoning
                    request_kwargs["include"] = ["reasoning.encrypted_content"]
            return request_kwargs

        dependency_key = "provider:openai"
        attempts = max(1, settings.PROVIDER_RETRY_ATTEMPTS)
        attempt = 1

        while attempt <= attempts:
            tool_calls_map: dict[str, dict[str, str]] = {}
            tool_call_fragment_counts: dict[str, int] = {}
            tool_calls_count = 0
            completed_summary_keys: set[tuple[str, int, int]] = set()
            continuation_items: list[dict[str, object]] = []
            continuation_attached = False
            pending_tool_error: StreamEvent | None = None
            emitted_event = False
            try:
                await dependency_circuit_breaker.before_call(dependency_key, "provider", "openai")

                stream = None
                compatibility_attempts_remaining = 3
                current_include_temperature = include_temperature
                current_include_reasoning = reasoning_requested
                while stream is None and compatibility_attempts_remaining > 0:
                    request_kwargs = build_request_kwargs(
                        current_include_temperature,
                        current_include_reasoning,
                    )
                    try:
                        stream = await client.responses.create(**request_kwargs)
                    except BadRequestError as error:
                        compatibility_attempts_remaining -= 1
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
                            if summary_requested:
                                logger.info(
                                    "provider_reasoning_summary_degraded",
                                    provider="openai",
                                    model=req.model,
                                    run_id=req.run_id,
                                    workspace_id=req.workspace_id,
                                    reason="unsupported_model",
                                )
                                yield StreamEvent(
                                    type="reasoning_summary_unavailable",
                                    provider="openai",
                                    reason="unsupported_model",
                                )
                                emitted_event = True
                            continue
                        raise
                if stream is None:
                    raise RuntimeError("OpenAI request failed after compatibility retries.")

                async for chunk in stream:
                    event_type = getattr(chunk, "type", "")

                    if event_type == "response.output_text.delta":
                        text = getattr(chunk, "delta", "") or ""
                        if text:
                            emitted_event = True
                            yield StreamEvent(type="delta", text=text)
                        continue

                    if event_type == "response.reasoning_summary_text.delta":
                        if not summary_requested:
                            continue
                        text = getattr(chunk, "delta", "") or ""
                        if text:
                            emitted_event = True
                            yield StreamEvent(
                                type="reasoning_summary_delta",
                                text=text,
                                provider="openai",
                            )
                        continue

                    if event_type == "response.reasoning_summary_text.done":
                        if not summary_requested:
                            continue
                        text = getattr(chunk, "text", "") or ""
                        if text:
                            completed_summary_keys.add((
                                str(getattr(chunk, "item_id", "") or ""),
                                int(getattr(chunk, "output_index", 0) or 0),
                                int(getattr(chunk, "summary_index", 0) or 0),
                            ))
                            emitted_event = True
                            yield StreamEvent(
                                type="reasoning_summary_completed",
                                text=text,
                                provider="openai",
                            )
                        continue

                    if event_type == "response.reasoning_summary_part.done":
                        if not summary_requested:
                            continue
                        part = getattr(chunk, "part", None)
                        text = getattr(part, "text", "") or ""
                        part_type = getattr(part, "type", "")
                        summary_key = (
                            str(getattr(chunk, "item_id", "") or ""),
                            int(getattr(chunk, "output_index", 0) or 0),
                            int(getattr(chunk, "summary_index", 0) or 0),
                        )
                        if (
                            part_type == "summary_text"
                            and text
                            and summary_key not in completed_summary_keys
                        ):
                            completed_summary_keys.add(summary_key)
                            emitted_event = True
                            yield StreamEvent(
                                type="reasoning_summary_completed",
                                text=text,
                                provider="openai",
                            )
                        continue

                    # Raw reasoning text is intentionally ignored.
                    if event_type.startswith("response.reasoning_text."):
                        continue

                    if event_type in {"response.output_item.added", "response.output_item.done"}:
                        item = getattr(chunk, "item", None)
                        if (
                            event_type == "response.output_item.done"
                            and getattr(item, "type", None) == "reasoning"
                        ):
                            continuation_items.append(
                                capture_openai_reasoning_input(item)
                            )
                        if getattr(item, "type", None) == "function_call":
                            raw_call_id = str(getattr(item, "call_id", "") or "")
                            item_id = str(getattr(item, "id", "") or raw_call_id)
                            incoming_name = str(getattr(item, "name", "") or "")
                            incoming_arguments = str(getattr(item, "arguments", "") or "")
                            if not item_id:
                                pending_tool_error = pending_tool_error or openai_tool_error_event(
                                    req,
                                    api_surface="responses",
                                    code="OPENAI_TOOL_CALL_INVALID",
                                    reason="missing call identity",
                                    tool=incoming_name or None,
                                    call_id=None,
                                    argument_bytes=len(incoming_arguments.encode("utf-8")),
                                    fragment_count=1,
                                )
                                continue
                            existing = tool_calls_map.get(item_id)
                            if (
                                existing
                                and raw_call_id
                                and existing.get("id")
                                and existing["id"] != raw_call_id
                            ):
                                pending_tool_error = pending_tool_error or openai_tool_error_event(
                                    req,
                                    api_surface="responses",
                                    code="OPENAI_TOOL_CALL_INVALID",
                                    reason="conflicting call identity",
                                    tool=str(existing.get("name") or incoming_name) or None,
                                    call_id=None,
                                    argument_bytes=len(
                                        existing.get("arguments", "").encode("utf-8")
                                    ),
                                    fragment_count=tool_call_fragment_counts.get(item_id, 1),
                                )
                                continue
                            tool_calls_map[item_id] = {
                                "id": raw_call_id or item_id,
                                "name": incoming_name,
                                "arguments": incoming_arguments,
                            }
                            tool_call_fragment_counts[item_id] = (
                                tool_call_fragment_counts.get(item_id, 0) + 1
                            )
                            if event_type == "response.output_item.done":
                                tc = tool_calls_map.get(item_id)
                                if tc and (not tc["id"] or not tc["name"]):
                                    argument_bytes = len(str(tc["arguments"]).encode("utf-8"))
                                    pending_tool_error = (
                                        pending_tool_error
                                        or openai_tool_error_event(
                                            req,
                                            api_surface="responses",
                                            code="OPENAI_TOOL_CALL_INVALID",
                                            reason="missing call identity",
                                            tool=str(tc["name"] or "") or None,
                                            call_id=str(tc["id"] or "") or None,
                                            argument_bytes=argument_bytes,
                                            fragment_count=tool_call_fragment_counts[item_id],
                                        )
                                    )
                                    continue
                                if tc and tc["name"] and not tc.get("yielded"):
                                    arguments = parse_openai_tool_arguments(str(tc["arguments"]))
                                    argument_bytes = len(str(tc["arguments"]).encode("utf-8"))
                                    if arguments is None:
                                        pending_tool_error = (
                                            pending_tool_error
                                            or openai_tool_error_event(
                                                req,
                                                api_surface="responses",
                                                code="OPENAI_TOOL_ARGUMENTS_INVALID",
                                                reason="malformed JSON arguments",
                                                call_id=str(tc["id"]),
                                                tool=str(tc["name"]),
                                                argument_bytes=argument_bytes,
                                                fragment_count=tool_call_fragment_counts[item_id],
                                            )
                                        )
                                        tc["yielded"] = "invalid"
                                        continue
                                    observe_openai_tool_arguments(
                                        "responses", "incremental", argument_bytes
                                    )
                                    tool_calls_count += 1
                                    emitted_event = True
                                    yield StreamEvent(
                                        type="tool_call",
                                        call_id=str(tc["id"]),
                                        tool=str(tc["name"]),
                                        arguments=arguments,
                                        provider_state=(
                                            {
                                                "provider": "openai",
                                                "data": {
                                                    "surface": "responses",
                                                    "items": continuation_items,
                                                },
                                            }
                                            if (
                                                continuation_items
                                                and not continuation_attached
                                            )
                                            else None
                                        ),
                                    )
                                    continuation_attached = bool(
                                        continuation_items
                                    ) or continuation_attached
                                    tc["yielded"] = "true"
                        continue

                    if event_type == "response.function_call_arguments.delta":
                        item_id = str(getattr(chunk, "item_id", "") or "")
                        if item_id in tool_calls_map:
                            tool_calls_map[item_id]["arguments"] += (
                                getattr(chunk, "delta", "") or ""
                            )
                            tool_call_fragment_counts[item_id] = (
                                tool_call_fragment_counts.get(item_id, 0) + 1
                            )
                        continue

                    if event_type == "response.function_call_arguments.done":
                        item_id = str(getattr(chunk, "item_id", "") or "")
                        if item_id in tool_calls_map:
                            tool_calls_map[item_id]["arguments"] = (
                                getattr(chunk, "arguments", "")
                                or tool_calls_map[item_id]["arguments"]
                            )
                            tool_call_fragment_counts[item_id] = (
                                tool_call_fragment_counts.get(item_id, 0) + 1
                            )
                        continue

                    if event_type == "response.completed":
                        response = getattr(chunk, "response", None)
                        usage_obj = getattr(response, "usage", None)
                        input_tokens = int(getattr(usage_obj, "input_tokens", 0) or 0)
                        output_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
                        output_details = getattr(usage_obj, "output_tokens_details", None)
                        reasoning_tokens = int(getattr(output_details, "reasoning_tokens", 0) or 0)
                        usage = {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "tool_calls": tool_calls_count,
                        }
                        if reasoning_tokens:
                            usage["reasoning_tokens"] = reasoning_tokens
                        if pending_tool_error is not None:
                            pending_tool_error.usage = usage
                            emitted_event = True
                            yield pending_tool_error
                            await dependency_circuit_breaker.record_success(dependency_key)
                            return
                        yield StreamEvent(
                            type="final",
                            usage=usage,
                        )
                        emitted_event = True
                        continue

                if pending_tool_error is not None:
                    emitted_event = True
                    yield pending_tool_error
                await dependency_circuit_breaker.record_success(dependency_key)
                return
            except CircuitOpenError as exc:
                logger.warning(
                    "provider_circuit_open",
                    provider="openai",
                    api_surface="responses",
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
                            backoff_seconds(settings.PROVIDER_RETRY_BACKOFF_MS, attempt)
                        )
                        attempt += 1
                        continue
                yield provider_failure_event(
                    exc,
                    fallback_code="OPENAI_ERROR",
                    retryable=retryable,
                )
                return


class OpenAIAdapter(LLMAdapter):
    """Routes normalized OpenAI requests to the configured outbound API surface."""

    async def stream(
        self,
        req: NormalizedLLMRequest,
        api_key: str,
    ) -> AsyncIterator[StreamEvent]:
        api_surface = settings.LLM_PROVIDER_OPENAI_API_SURFACE
        if api_surface == "responses":
            adapter: LLMAdapter = OpenAIResponsesAdapter()
        elif api_surface == "chat_completions":
            adapter = OpenAIChatCompletionsAdapter()
        else:
            raise ValueError(f"Unsupported OpenAI API surface: {api_surface}")

        async for event in adapter.stream(req, api_key):
            yield event
