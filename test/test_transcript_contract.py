"""Focused canonical transcript model and sequencing tests."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.llm.service import NormalizedLLMRequest
from app.llm.transcript import (
    PROVIDER_STATE_MAX_BYTES,
    AssistantTurn,
    ProviderContinuationState,
    ToolCallPart,
    ToolResult,
    ToolResultTurn,
    TranscriptTurn,
    UserTurn,
    public_transcript_payload,
    validate_transcript_sequence,
)


def valid_transcript():
    return [
        UserTurn(type="user", content="Inspect the workload."),
        AssistantTurn(
            type="assistant",
            content=[
                {"type": "text", "text": "I will inspect it."},
                {
                    "type": "tool_call",
                    "call_id": "call-1",
                    "name": "get_resource",
                    "arguments": {"name": "api"},
                    "provider_state": {
                        "provider": "gemini",
                        "data": {"thought_signature": "opaque"},
                    },
                },
            ],
        ),
        ToolResultTurn(
            type="tool_results",
            results=[
                ToolResult(
                    call_id="call-1",
                    name="get_resource",
                    result={"status": "ready"},
                    is_error=False,
                )
            ],
        ),
        AssistantTurn(
            type="assistant",
            content=[{"type": "text", "text": "The workload is ready."}],
        ),
    ]


def test_valid_transcript_round_trips_stably():
    adapter = TypeAdapter(list[TranscriptTurn])
    serialized = adapter.dump_json(valid_transcript())
    parsed = adapter.validate_json(serialized)
    assert adapter.dump_json(parsed) == serialized
    validate_transcript_sequence(parsed, "gemini")


@pytest.mark.parametrize(
    ("turns", "message"),
    [
        (
            [
                ToolResultTurn(
                    type="tool_results",
                    results=[
                        ToolResult(
                            call_id="orphan",
                            name="get_resource",
                            result={},
                            is_error=False,
                        )
                    ],
                )
            ],
            "no preceding",
        ),
        (
            [
                AssistantTurn(
                    type="assistant",
                    content=[
                        ToolCallPart(
                            type="tool_call",
                            call_id="call-1",
                            name="get_resource",
                            arguments={},
                        )
                    ],
                ),
                AssistantTurn(
                    type="assistant",
                    content=[{"type": "text", "text": "Skipped the result."}],
                ),
            ],
            "followed immediately",
        ),
        (
            [
                AssistantTurn(
                    type="assistant",
                    content=[
                        ToolCallPart(
                            type="tool_call",
                            call_id="call-1",
                            name="get_resource",
                            arguments={},
                        )
                    ],
                ),
                ToolResultTurn(
                    type="tool_results",
                    results=[
                        ToolResult(
                            call_id="call-1",
                            name="other_tool",
                            result={},
                            is_error=True,
                        )
                    ],
                ),
            ],
            "does not match",
        ),
    ],
)
def test_invalid_sequences_fail_precisely(turns, message):
    with pytest.raises(ValueError, match=message):
        validate_transcript_sequence(turns, "openai")


def test_duplicate_calls_and_results_fail():
    calls = AssistantTurn(
        type="assistant",
        content=[
            ToolCallPart(
                type="tool_call",
                call_id="duplicate",
                name="one",
                arguments={},
            ),
            ToolCallPart(
                type="tool_call",
                call_id="duplicate",
                name="two",
                arguments={},
            ),
        ],
    )
    with pytest.raises(ValueError, match="duplicate"):
        validate_transcript_sequence([calls], "openai")


def test_provider_state_is_bounded_provider_bound_and_private():
    transcript = valid_transcript()
    with pytest.raises(ValueError, match="issuing provider"):
        validate_transcript_sequence(transcript, "openai")
    public = public_transcript_payload(transcript)
    assert "provider_state" not in public[1]["content"][1]
    with pytest.raises(ValidationError, match="exceeds"):
        ProviderContinuationState(
            provider="gemini",
            data={"signature": "x" * (PROVIDER_STATE_MAX_BYTES + 1)},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "system", "content": "trusted?"},
        {"type": "developer", "content": "trusted?"},
        {"type": "user", "content": "ok", "unknown": True},
    ],
)
def test_unknown_or_system_like_turns_are_rejected(payload):
    adapter = TypeAdapter(TranscriptTurn)
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


def test_arguments_must_be_an_object_and_names_are_safe():
    with pytest.raises(ValidationError):
        ToolCallPart.model_validate(
            {
                "type": "tool_call",
                "call_id": "call-1",
                "name": "get_resource",
                "arguments": [],
            }
        )


def test_legacy_messages_request_is_rejected():
    with pytest.raises(ValidationError):
        NormalizedLLMRequest.model_validate(
            {
                "run_id": "run-1",
                "workspace_id": "workspace-1",
                "target_id": "target-1",
                "target_type": "kubernetes",
                "session_id": "session-1",
                "provider": "openai",
                "model": "gpt-test",
                "runtime_instruction": "You are AcornOps.",
                "transcript": [{"type": "user", "content": "hello"}],
                "messages": [{"role": "user", "content": "legacy"}],
            }
        )
    with pytest.raises(ValidationError, match="must match"):
        ToolCallPart(
            type="tool_call",
            call_id="call-1",
            name="unsafe name",
            arguments={},
        )
