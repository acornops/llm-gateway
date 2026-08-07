"""Content-safe OpenAI tool argument diagnostics shared by both API surfaces."""

import structlog

from app.llm.service import NormalizedLLMRequest, StreamEvent
from app.observability.metrics import (
    GATEWAY_LLM_TOOL_ARGUMENT_BYTES,
    GATEWAY_LLM_TOOL_ARGUMENT_EVENTS_TOTAL,
)

logger = structlog.get_logger()


def observe_openai_tool_arguments(
    api_surface: str,
    outcome: str,
    argument_bytes: int,
) -> None:
    GATEWAY_LLM_TOOL_ARGUMENT_BYTES.labels(
        provider="openai",
        api_surface=api_surface,
    ).observe(argument_bytes)
    GATEWAY_LLM_TOOL_ARGUMENT_EVENTS_TOTAL.labels(
        provider="openai",
        api_surface=api_surface,
        outcome=outcome,
    ).inc()


def openai_tool_error_event(
    req: NormalizedLLMRequest,
    *,
    api_surface: str,
    code: str,
    reason: str,
    tool: str | None,
    call_id: str | None,
    argument_bytes: int,
    fragment_count: int,
    usage: dict[str, int] | None = None,
) -> StreamEvent:
    outcome = "invalid_json" if code == "OPENAI_TOOL_ARGUMENTS_INVALID" else "invalid_call"
    observe_openai_tool_arguments(api_surface, outcome, argument_bytes)
    logger.warning(
        "provider_tool_arguments_invalid"
        if outcome == "invalid_json"
        else "provider_tool_call_invalid",
        provider="openai",
        api_surface=api_surface,
        model=req.model,
        run_id=req.run_id,
        workspace_id=req.workspace_id,
        tool=tool,
        fragment_count=fragment_count,
        argument_bytes=argument_bytes,
        reason=reason,
    )
    return StreamEvent(
        type="error",
        code=code,
        message=(
            "Provider returned malformed JSON tool arguments; no tool was executed"
            if outcome == "invalid_json"
            else "Provider returned an invalid tool call; no tool was executed"
        ),
        call_id=call_id or None,
        tool=tool or None,
        retryable=outcome == "invalid_json",
        usage=usage,
    )
