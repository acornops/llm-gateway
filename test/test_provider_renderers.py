"""Exact provider payload fixtures for canonical transcript scenarios."""

import json

import pytest

from app.llm.renderers import (
    render_anthropic_messages,
    render_gemini_contents,
    render_openai_chat_messages,
    render_openai_responses_input,
)
from app.llm.service import NormalizedLLMRequest


def _request(provider: str, transcript: list[dict]) -> NormalizedLLMRequest:
    return NormalizedLLMRequest(
        run_id="11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
        target_id="33333333-3333-4333-8333-333333333333",
        target_type="kubernetes",
        session_id="44444444-4444-4444-8444-444444444444",
        provider=provider,
        model="fixture-model",
        runtime_instruction="Trusted runtime instruction.",
        transcript=transcript,
    )


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "type": "tool_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _result(
    call_id: str,
    name: str,
    result: object,
    is_error: bool = False,
) -> dict:
    return {
        "call_id": call_id,
        "name": name,
        "result": result,
        "is_error": is_error,
    }


SCENARIOS = {
    "text_only": [
        {"type": "user", "content": "Hello."},
        {
            "type": "assistant",
            "content": [{"type": "text", "text": "Hi."}],
        },
    ],
    "success": [
        {"type": "user", "content": "Inspect."},
        {
            "type": "assistant",
            "content": [_call("call-1", "inspect", {"name": "api"})],
        },
        {
            "type": "tool_results",
            "results": [_result("call-1", "inspect", {"ready": True})],
        },
    ],
    "error": [
        {"type": "user", "content": "Inspect."},
        {
            "type": "assistant",
            "content": [_call("call-1", "inspect", {"name": "api"})],
        },
        {
            "type": "tool_results",
            "results": [
                _result(
                    "call-1",
                    "inspect",
                    {"code": "NOT_FOUND"},
                    is_error=True,
                )
            ],
        },
    ],
    "text_and_call": [
        {"type": "user", "content": "Inspect."},
        {
            "type": "assistant",
            "content": [
                {"type": "text", "text": "Checking now."},
                _call("call-1", "inspect", {"name": "api"}),
            ],
        },
        {
            "type": "tool_results",
            "results": [_result("call-1", "inspect", {"ready": True})],
        },
    ],
    "parallel": [
        {"type": "user", "content": "Inspect both."},
        {
            "type": "assistant",
            "content": [
                _call("call-1", "inspect", {"name": "api"}),
                _call("call-2", "inspect", {"name": "worker"}),
            ],
        },
        {
            "type": "tool_results",
            "results": [
                _result("call-1", "inspect", {"ready": True}),
                _result("call-2", "inspect", {"ready": False}),
            ],
        },
    ],
    "sequential": [
        {"type": "user", "content": "Inspect then verify."},
        {
            "type": "assistant",
            "content": [_call("call-1", "inspect", {"name": "api"})],
        },
        {
            "type": "tool_results",
            "results": [_result("call-1", "inspect", {"ready": False})],
        },
        {
            "type": "assistant",
            "content": [_call("call-2", "verify", {"name": "api"})],
        },
        {
            "type": "tool_results",
            "results": [_result("call-2", "verify", {"ready": True})],
        },
    ],
    "read_write_verify": [
        {"type": "user", "content": "Diagnose and repair api."},
        {
            "type": "assistant",
            "content": [
                {"type": "text", "text": "Inspecting current state."},
                _call("read-1", "inspect", {"name": "api"}),
            ],
        },
        {
            "type": "tool_results",
            "results": [
                _result("read-1", "inspect", {"ready": False})
            ],
        },
        {
            "type": "assistant",
            "content": [
                _call("write-1", "restart", {"name": "api"})
            ],
        },
        {
            "type": "tool_results",
            "results": [
                _result("write-1", "restart", {"operation_id": "op-1"})
            ],
        },
        {
            "type": "assistant",
            "content": [
                _call("verify-1", "inspect", {"name": "api", "fresh": True})
            ],
        },
        {
            "type": "tool_results",
            "results": [
                _result("verify-1", "inspect", {"ready": True})
            ],
        },
    ],
}


def _result_text(result: dict) -> str:
    return json.dumps(
        {"is_error": result["is_error"], "result": result["result"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _expected_openai_responses(transcript: list[dict]) -> list[dict]:
    expected: list[dict] = []
    for turn in transcript:
        if turn["type"] == "user":
            expected.append({"role": "user", "content": turn["content"]})
        elif turn["type"] == "assistant":
            for part in turn["content"]:
                if part["type"] == "text":
                    expected.append(
                        {"role": "assistant", "content": part["text"]}
                    )
                else:
                    expected.append(
                        {
                            "type": "function_call",
                            "call_id": part["call_id"],
                            "name": part["name"],
                            "arguments": json.dumps(
                                part["arguments"],
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        }
                    )
        else:
            expected.extend(
                {
                    "type": "function_call_output",
                    "call_id": result["call_id"],
                    "output": _result_text(result),
                }
                for result in turn["results"]
            )
    return expected


def _expected_openai_chat(transcript: list[dict]) -> list[dict]:
    expected: list[dict] = [
        {"role": "system", "content": "Trusted runtime instruction."}
    ]
    for turn in transcript:
        if turn["type"] == "user":
            expected.append({"role": "user", "content": turn["content"]})
        elif turn["type"] == "assistant":
            text = "".join(
                part["text"]
                for part in turn["content"]
                if part["type"] == "text"
            )
            calls = [
                part for part in turn["content"] if part["type"] == "tool_call"
            ]
            message = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call["call_id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                call["arguments"],
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        },
                    }
                    for call in calls
                ]
            expected.append(message)
        else:
            expected.extend(
                {
                    "role": "tool",
                    "tool_call_id": result["call_id"],
                    "content": _result_text(result),
                }
                for result in turn["results"]
            )
    return expected


def _expected_anthropic(transcript: list[dict]) -> list[dict]:
    expected: list[dict] = []
    for turn in transcript:
        if turn["type"] == "user":
            expected.append({"role": "user", "content": turn["content"]})
        elif turn["type"] == "assistant":
            content = []
            for part in turn["content"]:
                if part["type"] == "text":
                    content.append({"type": "text", "text": part["text"]})
                else:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": part["call_id"],
                            "name": part["name"],
                            "input": part["arguments"],
                        }
                    )
            expected.append({"role": "assistant", "content": content})
        else:
            expected.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result["call_id"],
                            "content": json.dumps(
                                result["result"],
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            **(
                                {"is_error": True}
                                if result["is_error"]
                                else {}
                            ),
                        }
                        for result in turn["results"]
                    ],
                }
            )
    return expected


def _expected_gemini(transcript: list[dict]) -> list[dict]:
    expected: list[dict] = []
    for turn in transcript:
        if turn["type"] == "user":
            expected.append(
                {"role": "user", "parts": [{"text": turn["content"]}]}
            )
        elif turn["type"] == "assistant":
            parts = []
            for part in turn["content"]:
                if part["type"] == "text":
                    parts.append({"text": part["text"]})
                else:
                    parts.append(
                        {
                            "function_call": {
                                "id": part["call_id"],
                                "name": part["name"],
                                "args": part["arguments"],
                            }
                        }
                    )
            expected.append({"role": "model", "parts": parts})
        else:
            expected.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "id": result["call_id"],
                                "name": result["name"],
                                "response": {
                                    (
                                        "error"
                                        if result["is_error"]
                                        else "output"
                                    ): result["result"]
                                },
                            }
                        }
                        for result in turn["results"]
                    ],
                }
            )
    return expected


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_openai_responses_exact_payloads(scenario: str):
    transcript = SCENARIOS[scenario]
    req = _request("openai", transcript)
    assert render_openai_responses_input(req) == _expected_openai_responses(
        transcript
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_openai_chat_completions_exact_payloads(scenario: str):
    transcript = SCENARIOS[scenario]
    req = _request("openai", transcript)
    assert render_openai_chat_messages(req) == _expected_openai_chat(transcript)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_anthropic_exact_payloads(scenario: str):
    transcript = SCENARIOS[scenario]
    req = _request("anthropic", transcript)
    assert render_anthropic_messages(req) == _expected_anthropic(transcript)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_gemini_exact_payloads(scenario: str):
    transcript = SCENARIOS[scenario]
    req = _request("gemini", transcript)
    assert render_gemini_contents(req) == _expected_gemini(transcript)


def test_provider_continuation_state_renders_only_at_native_boundary():
    openai = _request(
        "openai",
        [
            {"type": "user", "content": "Inspect."},
            {
                "type": "assistant",
                "content": [
                    {
                        **_call("call-1", "inspect", {}),
                        "provider_state": {
                            "provider": "openai",
                            "data": {
                                "surface": "responses",
                                "items": [
                                    {
                                        "type": "reasoning",
                                        "id": "rs-1",
                                        "summary": [],
                                        "encrypted_content": "opaque",
                                        "status": "completed",
                                        "object": "response.reasoning_item",
                                    }
                                ],
                            },
                        },
                    }
                ],
            },
            {
                "type": "tool_results",
                "results": [_result("call-1", "inspect", {})],
            },
        ],
    )
    assert render_openai_responses_input(openai)[1] == {
        "type": "reasoning",
        "id": "rs-1",
        "summary": [],
        "encrypted_content": "opaque",
    }

    anthropic = _request(
        "anthropic",
        [
            {"type": "user", "content": "Inspect."},
            {
                "type": "assistant",
                "content": [
                    {
                        **_call("call-1", "inspect", {}),
                        "provider_state": {
                            "provider": "anthropic",
                            "data": {
                                "blocks": [
                                    {
                                        "type": "thinking",
                                        "thinking": "opaque provider block",
                                        "signature": "signature",
                                    }
                                ]
                            },
                        },
                    }
                ],
            },
            {
                "type": "tool_results",
                "results": [_result("call-1", "inspect", {})],
            },
        ],
    )
    assert (
        render_anthropic_messages(anthropic)[1]["content"][0]["type"]
        == "thinking"
    )

    gemini = _request(
        "gemini",
        [
            {"type": "user", "content": "Inspect."},
            {
                "type": "assistant",
                "content": [
                    {
                        **_call("call-1", "inspect", {}),
                        "provider_state": {
                            "provider": "gemini",
                            "data": {"thought_signature": "b3BhcXVl"},
                        },
                    }
                ],
            },
            {
                "type": "tool_results",
                "results": [_result("call-1", "inspect", {})],
            },
        ],
    )
    assert (
        render_gemini_contents(gemini)[1]["parts"][0]["thought_signature"]
        == b"opaque"
    )
