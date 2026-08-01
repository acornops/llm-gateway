"""Pure provider-native renderers for the canonical transcript."""

import base64
import json
from typing import Any

from app.llm.openai_continuation import project_openai_reasoning_input
from app.llm.service import NormalizedLLMRequest
from app.llm.transcript import (
    AssistantTurn,
    TextPart,
    ToolCallPart,
    ToolResultTurn,
    UserTurn,
)


def _result_payload(result: Any, is_error: bool) -> str:
    return json.dumps(
        {"is_error": is_error, "result": result},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _provider_data(call: ToolCallPart, provider: str) -> dict[str, Any] | None:
    state = call.provider_state
    if state is None:
        return None
    if state.provider != provider:
        raise ValueError(
            "provider continuation state may only be sent to its issuing provider"
        )
    return state.data


def render_openai_responses_input(req: NormalizedLLMRequest) -> list[dict[str, Any]]:
    """Render canonical turns into Responses API input items."""

    items: list[dict[str, Any]] = []
    for turn in req.transcript:
        if isinstance(turn, UserTurn):
            items.append({"role": "user", "content": turn.content})
            continue
        if isinstance(turn, AssistantTurn):
            for part in turn.content:
                if isinstance(part, TextPart):
                    items.append({"role": "assistant", "content": part.text})
                    continue
                provider_data = _provider_data(part, "openai")
                if provider_data:
                    if provider_data.get("surface") != "responses":
                        raise ValueError(
                            "OpenAI continuation state is not for the Responses surface"
                        )
                    continuation_items = provider_data.get("items")
                    if not isinstance(continuation_items, list):
                        raise ValueError(
                            "OpenAI Responses continuation items must be a list"
                        )
                    for item in continuation_items:
                        if not isinstance(item, dict):
                            raise ValueError(
                                "OpenAI Responses continuation contains an "
                                "unsupported item"
                            )
                        items.append(project_openai_reasoning_input(item))
                items.append(
                    {
                        "type": "function_call",
                        "call_id": part.call_id,
                        "name": part.name,
                        "arguments": json.dumps(
                            part.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    }
                )
            continue
        if isinstance(turn, ToolResultTurn):
            items.extend(
                {
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": _result_payload(result.result, result.is_error),
                }
                for result in turn.results
            )
    return items


def render_openai_chat_messages(req: NormalizedLLMRequest) -> list[dict[str, Any]]:
    """Render canonical turns into Chat Completions messages."""

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": req.runtime_instruction}
    ]
    for turn in req.transcript:
        if isinstance(turn, UserTurn):
            messages.append({"role": "user", "content": turn.content})
            continue
        if isinstance(turn, AssistantTurn):
            text = "".join(
                part.text for part in turn.content if isinstance(part, TextPart)
            )
            calls = [
                part for part in turn.content if isinstance(part, ToolCallPart)
            ]
            message: dict[str, Any] = {
                "role": "assistant",
                "content": text or None,
            }
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        },
                    }
                    for call in calls
                ]
            messages.append(message)
            continue
        if isinstance(turn, ToolResultTurn):
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": _result_payload(result.result, result.is_error),
                }
                for result in turn.results
            )
    return messages


def render_anthropic_messages(req: NormalizedLLMRequest) -> list[dict[str, Any]]:
    """Render canonical turns into Anthropic Messages content blocks."""

    messages: list[dict[str, Any]] = []
    for turn in req.transcript:
        if isinstance(turn, UserTurn):
            messages.append({"role": "user", "content": turn.content})
            continue
        if isinstance(turn, AssistantTurn):
            content: list[dict[str, Any]] = []
            for part in turn.content:
                if isinstance(part, TextPart):
                    content.append({"type": "text", "text": part.text})
                    continue
                provider_data = _provider_data(part, "anthropic")
                if provider_data:
                    blocks = provider_data.get("blocks")
                    if not isinstance(blocks, list):
                        raise ValueError(
                            "Anthropic continuation blocks must be a list"
                        )
                    for block in blocks:
                        if (
                            not isinstance(block, dict)
                            or block.get("type")
                            not in {"thinking", "redacted_thinking"}
                        ):
                            raise ValueError(
                                "Anthropic continuation contains an unsupported block"
                            )
                        content.append(block)
                content.append(
                    {
                        "type": "tool_use",
                        "id": part.call_id,
                        "name": part.name,
                        "input": part.arguments,
                    }
                )
            messages.append({"role": "assistant", "content": content})
            continue
        if isinstance(turn, ToolResultTurn):
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": json.dumps(
                                result.result,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            **({"is_error": True} if result.is_error else {}),
                        }
                        for result in turn.results
                    ],
                }
            )
    return messages


def _decode_gemini_signature(data: dict[str, Any]) -> bytes | None:
    encoded = data.get("thought_signature")
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise ValueError("Gemini thought signature must be base64 text")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("Gemini thought signature is not valid base64") from exc


def render_gemini_contents(req: NormalizedLLMRequest) -> list[dict[str, Any]]:
    """Render canonical turns into Gemini Content/Part dictionaries."""

    contents: list[dict[str, Any]] = []
    for turn in req.transcript:
        if isinstance(turn, UserTurn):
            contents.append(
                {"role": "user", "parts": [{"text": turn.content}]}
            )
            continue
        if isinstance(turn, AssistantTurn):
            parts: list[dict[str, Any]] = []
            for part in turn.content:
                if isinstance(part, TextPart):
                    parts.append({"text": part.text})
                    continue
                rendered: dict[str, Any] = {
                    "function_call": {
                        "id": part.call_id,
                        "name": part.name,
                        "args": part.arguments,
                    }
                }
                provider_data = _provider_data(part, "gemini")
                if provider_data:
                    signature = _decode_gemini_signature(provider_data)
                    if signature is not None:
                        rendered["thought_signature"] = signature
                parts.append(rendered)
            contents.append({"role": "model", "parts": parts})
            continue
        if isinstance(turn, ToolResultTurn):
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "id": result.call_id,
                                "name": result.name,
                                "response": {
                                    "error" if result.is_error else "output": (
                                        result.result
                                    )
                                },
                            }
                        }
                        for result in turn.results
                    ],
                }
            )
    return contents
