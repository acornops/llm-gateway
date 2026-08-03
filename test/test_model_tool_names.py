import pytest
from fastapi import HTTPException

from app.api.handlers_llm_stream import (
    _authorization_checked_tool_names,
    _select_deterministic_tool,
    _validate_stream_tool_names,
)
from app.llm.adapters.common import (
    build_anthropic_tools,
    build_gemini_tools,
    build_openai_chat_completion_tools,
    build_openai_response_tools,
)
from app.llm.service import NormalizedLLMRequest, ToolSpec


def _request(*tools: ToolSpec) -> NormalizedLLMRequest:
    return NormalizedLLMRequest(
        run_id="11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
        target_id="33333333-3333-4333-8333-333333333333",
        target_type="kubernetes",
        session_id="44444444-4444-4444-8444-444444444444",
        provider="openai",
        model="gpt-test",
        runtime_instruction="Use the authorized tools.",
        transcript=[{"type": "user", "content": "Inspect the target."}],
        tools=list(tools),
    )


def test_all_provider_declarations_use_the_optional_readable_name():
    spec = ToolSpec(
        name="m_123098sa90d80s9f_get_target_we92809",
        model_name="get_target",
    )

    assert build_openai_response_tools([spec])[0]["name"] == "get_target"
    assert (
        build_openai_chat_completion_tools([spec])[0]["function"]["name"]
        == "get_target"
    )
    assert build_anthropic_tools([spec])[0]["name"] == "get_target"
    assert (
        build_gemini_tools([spec])[0]["function_declarations"][0]["name"]
        == "get_target"
    )


def test_omitted_model_name_preserves_the_existing_contract():
    spec = ToolSpec(name="list_pods")

    assert spec.provider_name == "list_pods"
    assert build_openai_response_tools([spec])[0]["name"] == "list_pods"


def test_authorization_still_checks_the_internal_tool_identity():
    req = _request(ToolSpec(name="opaque_get_target", model_name="get_target"))

    assert _authorization_checked_tool_names(req) == ["opaque_get_target"]
    assert _select_deterministic_tool(req) == "get_target"


def test_duplicate_readable_names_are_rejected_case_insensitively():
    req = _request(
        ToolSpec(name="opaque-a", model_name="search"),
        ToolSpec(name="opaque-b", model_name="SEARCH"),
    )

    with pytest.raises(HTTPException) as exc_info:
        _validate_stream_tool_names(req)

    assert exc_info.value.status_code == 400
    assert "Duplicate provider tool name" in str(exc_info.value.detail)


def test_reserved_internal_prefix_cannot_be_smuggled_through_model_name():
    req = _request(
        ToolSpec(name="opaque-safe-name", model_name="_acornops_custom_internal")
    )

    with pytest.raises(HTTPException) as exc_info:
        _validate_stream_tool_names(req)

    assert exc_info.value.status_code == 403


def test_internal_model_only_tool_cannot_be_renamed():
    req = _request(
        ToolSpec(name="_acornops_load_skill", model_name="load_anything")
    )

    with pytest.raises(HTTPException) as exc_info:
        _validate_stream_tool_names(req)

    assert exc_info.value.status_code == 403
