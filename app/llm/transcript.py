"""Provider-neutral model transcript contracts and sequence validation."""

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderName = Literal["openai", "anthropic", "gemini"]
PROVIDER_STATE_MAX_BYTES = 32 * 1024
TOOL_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,62}")


def _json_bytes(value: Any) -> int:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-compatible") from exc
    return len(encoded)


class StrictTranscriptModel(BaseModel):
    """Forbid silent admission of fields outside the canonical contract."""

    model_config = ConfigDict(extra="forbid")


class ProviderContinuationState(StrictTranscriptModel):
    """Bounded opaque state that may only return to its issuing provider."""

    provider: ProviderName
    data: dict[str, Any]

    @model_validator(mode="after")
    def validate_bounded_json(self):
        size = _json_bytes(self.data)
        if not self.data:
            raise ValueError("provider continuation data must not be empty")
        if size > PROVIDER_STATE_MAX_BYTES:
            raise ValueError(
                f"provider continuation data exceeds {PROVIDER_STATE_MAX_BYTES} bytes"
            )
        return self


class TextPart(StrictTranscriptModel):
    type: Literal["text"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assistant text must not be blank")
        return value


class ToolCallPart(StrictTranscriptModel):
    type: Literal["tool_call"]
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=63)
    arguments: dict[str, Any]
    provider_state: ProviderContinuationState | None = None

    @field_validator("name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        if not TOOL_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "tool name must match ^[A-Za-z_][A-Za-z0-9_-]{0,62}$"
            )
        return value

    @field_validator("arguments")
    @classmethod
    def validate_arguments_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _json_bytes(value)
        return value


AssistantPart = Annotated[TextPart | ToolCallPart, Field(discriminator="type")]


class UserTurn(StrictTranscriptModel):
    type: Literal["user"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user content must not be blank")
        return value


class AssistantTurn(StrictTranscriptModel):
    type: Literal["assistant"]
    content: list[AssistantPart] = Field(min_length=1)


class ToolResult(StrictTranscriptModel):
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=63)
    result: Any
    is_error: bool

    @field_validator("name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        if not TOOL_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "tool name must match ^[A-Za-z_][A-Za-z0-9_-]{0,62}$"
            )
        return value

    @field_validator("result")
    @classmethod
    def validate_result_json(cls, value: Any) -> Any:
        _json_bytes(value)
        return value


class ToolResultTurn(StrictTranscriptModel):
    type: Literal["tool_results"]
    results: list[ToolResult] = Field(min_length=1)


TranscriptTurn = Annotated[
    UserTurn | AssistantTurn | ToolResultTurn,
    Field(discriminator="type"),
]


def validate_transcript_sequence(
    transcript: list[TranscriptTurn],
    provider: ProviderName,
) -> None:
    """Validate call/result causality and provider-bound opaque state."""

    seen_call_ids: set[str] = set()
    pending_calls: list[ToolCallPart] = []

    for turn in transcript:
        if pending_calls:
            if not isinstance(turn, ToolResultTurn):
                raise ValueError(
                    "assistant tool calls must be followed immediately by one "
                    "grouped tool_results turn"
                )
            expected_ids = [call.call_id for call in pending_calls]
            actual_ids = [result.call_id for result in turn.results]
            if actual_ids != expected_ids:
                raise ValueError(
                    "tool results must match the preceding assistant calls "
                    "exactly and preserve call order"
                )
            expected_names = {
                call.call_id: call.name
                for call in pending_calls
            }
            for result in turn.results:
                if result.name != expected_names[result.call_id]:
                    raise ValueError(
                        f"tool result name for call_id '{result.call_id}' "
                        "does not match the preceding tool call"
                    )
            pending_calls = []
            continue

        if isinstance(turn, ToolResultTurn):
            raise ValueError("tool_results turn has no preceding assistant tool calls")
        if not isinstance(turn, AssistantTurn):
            continue

        calls = [part for part in turn.content if isinstance(part, ToolCallPart)]
        for call in calls:
            if call.call_id in seen_call_ids:
                raise ValueError(f"duplicate tool call_id '{call.call_id}'")
            seen_call_ids.add(call.call_id)
            if call.provider_state and call.provider_state.provider != provider:
                raise ValueError(
                    "provider continuation state may only be sent to its "
                    "issuing provider"
                )
        pending_calls = calls

    if pending_calls:
        raise ValueError(
            "transcript ends with unresolved assistant tool calls"
        )


def public_transcript_payload(
    transcript: list[TranscriptTurn],
) -> list[dict[str, Any]]:
    """Serialize transcript content without internal provider continuation state."""

    payload: list[dict[str, Any]] = []
    for turn in transcript:
        data = turn.model_dump()
        if isinstance(turn, AssistantTurn):
            for part in data["content"]:
                part.pop("provider_state", None)
        payload.append(data)
    return payload
