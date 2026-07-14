import json

from app.api.handlers_tool_call import _normalize_tool_response, _tool_transport_error_response
from app.mcp.transports.http_transport import McpToolTransportError

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["schemaVersion", "data"],
    "properties": {
        "schemaVersion": {"const": "acornops.full-tool-result.v1"},
        "data": {},
    },
    "additionalProperties": False,
}


def producer_result(context, data, *, artifact_policy="always"):
    context_text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": context_text}],
        "structuredContent": {"schemaVersion": "acornops.full-tool-result.v1", "data": data},
        "_meta": {"acornops.dev/result": {
            "contextSchemaVersion": "v1",
            "artifactPolicy": artifact_policy,
            "contextBytes": len(context_text.encode()),
            "originalBytes": len(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            ),
        }},
        "isError": False,
    }


def test_normalizes_trusted_producer_projection_and_full_result():
    context = {
        "schemaVersion": "acornops.model-context.v1",
        "tool": "get_resource",
        "status": "success",
        "summary": "Resolved the owning Deployment.",
        "data": {"remediationTarget": {"kind": "Deployment", "name": "api"}},
        "omissions": [],
    }
    response = _normalize_tool_response(
        producer_result(context, {"metadata": {"name": "api"}, "large": "x" * 2000}),
        trusted_builtin=True,
        output_schema=OUTPUT_SCHEMA,
        artifact_policy="always",
        expected_tool="get_resource",
    )
    assert response.model_context == context
    assert response.full_result["metadata"]["name"] == "api"
    assert response.context_meta["strategy"] == "producer_projection"
    assert response.artifact_eligible is True


def test_rejects_invalid_trusted_projection_without_artifact_retention():
    response = _normalize_tool_response(
        {
            "content": [{"type": "text", "text": "not-json"}],
            "structuredContent": {"unexpected": True},
            "_meta": {"acornops.dev/result": {"artifactPolicy": "always"}},
        },
        trusted_builtin=True,
        output_schema=OUTPUT_SCHEMA,
        artifact_policy="always",
    )
    assert response.is_error is True
    assert response.model_context["code"] == "TOOL_RESULT_SCHEMA_INVALID"
    assert response.artifact_eligible is False


def test_untrusted_structured_tool_is_never_artifact_eligible():
    response = _normalize_tool_response(
        {
            "content": [{"type": "text", "text": '{"status":"ok"}'}],
            "structuredContent": {"sensitive": "full"},
            "_meta": {"acornops.dev/result": {"artifactPolicy": "always"}},
        },
        trusted_builtin=False,
    )
    assert response.artifact_eligible is False


def test_untrusted_multi_block_content_is_preserved_for_structural_fallback():
    content = [
        {"type": "text", "text": '{"first":true}'},
        {"type": "text", "text": "remaining evidence"},
    ]
    response = _normalize_tool_response(
        {"content": content, "isError": False}, trusted_builtin=False
    )
    assert response.model_context == content


def test_untrusted_malformed_text_remains_structured_content_for_fallback():
    content = [{"type": "text", "text": "{malformed"}]
    response = _normalize_tool_response(
        {"content": content, "isError": True}, trusted_builtin=False
    )
    assert response.model_context == content
    assert response.is_error is True


def test_untrusted_malformed_mcp_envelope_becomes_a_contract_error():
    response = _normalize_tool_response(
        {"unexpected": True, "isError": "false"}, trusted_builtin=False
    )
    assert response.is_error is True
    assert response.full_result["code"] == "TOOL_RESULT_CONTRACT_INVALID"
    assert response.context_meta["strategy"] == "contract_error"


def test_trusted_tool_with_advertised_schema_cannot_use_generic_fallback():
    response = _normalize_tool_response(
        {"content": [{"type": "text", "text": '{"status":"ok"}'}]},
        trusted_builtin=True,
        output_schema=OUTPUT_SCHEMA,
        artifact_policy="always",
    )
    assert response.is_error is True
    assert response.context_meta["strategy"] == "schema_error"


def test_trusted_tool_without_advertised_schema_fails_closed():
    context = {
        "schemaVersion": "acornops.model-context.v1", "tool": "get_resource",
        "status": "success", "summary": "Inspected Pod default/api.", "data": {}, "omissions": [],
    }
    response = _normalize_tool_response(
        producer_result(context, {"metadata": {"name": "api"}}),
        trusted_builtin=True,
        output_schema=None,
        artifact_policy="always",
        expected_tool="get_resource",
    )
    assert response.is_error is True
    assert response.context_meta["strategy"] == "schema_error"


def test_trusted_tool_rejects_mismatched_tool_identity_and_invalid_size_metadata():
    context = {
        "schemaVersion": "acornops.model-context.v1", "tool": "list_resources",
        "status": "success", "summary": "Inspected resources.", "data": {}, "omissions": [],
    }
    payload = producer_result(context, {"items": []})
    payload["_meta"]["acornops.dev/result"]["originalBytes"] = "large"
    response = _normalize_tool_response(
        payload, trusted_builtin=True, output_schema=OUTPUT_SCHEMA,
        artifact_policy="always", expected_tool="get_resource",
    )
    assert response.is_error is True
    assert response.context_meta["strategy"] == "schema_error"


def test_trusted_tool_rejects_inconsistent_error_status_and_context_size():
    context = {
        "schemaVersion": "acornops.model-context.v1", "tool": "get_resource",
        "status": "error", "summary": "Lookup failed.", "data": {}, "omissions": [],
    }
    payload = producer_result(context, {"code": "NOT_FOUND"})
    payload["_meta"]["acornops.dev/result"]["contextBytes"] += 1
    response = _normalize_tool_response(
        payload, trusted_builtin=True, output_schema=OUTPUT_SCHEMA,
        artifact_policy="always", expected_tool="get_resource",
    )
    assert response.is_error is True
    assert response.context_meta["strategy"] == "schema_error"


def test_trusted_tool_rejects_non_text_model_context_block():
    context = {
        "schemaVersion": "acornops.model-context.v1", "tool": "get_resource",
        "status": "success", "summary": "Inspected Pod default/api.",
        "data": {}, "omissions": [],
    }
    payload = producer_result(context, {"metadata": {"name": "api"}})
    payload["content"][0]["type"] = "image"
    response = _normalize_tool_response(
        payload, trusted_builtin=True, output_schema=OUTPUT_SCHEMA,
        artifact_policy="always", expected_tool="get_resource",
    )
    assert response.is_error is True
    assert response.context_meta["strategy"] == "schema_error"


def test_trusted_result_accepts_javascript_and_python_number_format_differences():
    context = {
        "schemaVersion": "acornops.model-context.v1", "tool": "get_resource",
        "status": "success", "summary": "Inspected Pod default/api.",
        "data": {"ratio": 1e-7}, "omissions": [],
    }
    payload = producer_result(context, {"ratio": 1e-7})
    payload["_meta"]["acornops.dev/result"]["originalBytes"] += 1
    response = _normalize_tool_response(
        payload, trusted_builtin=True, output_schema=OUTPUT_SCHEMA,
        artifact_policy="always", expected_tool="get_resource",
    )
    assert response.is_error is False


def test_ambiguous_third_party_write_transport_failure_is_not_retryable():
    error = McpToolTransportError(
        {"isError": True, "content": [{"type": "text", "text": "MCP server timeout"}]},
        code="MCP_TOOL_TIMEOUT",
        dispatch_outcome="unknown",
        retryable=True,
    )
    response = _tool_transport_error_response(error, "write")
    assert response.full_result["outcome"] == "unknown"
    assert response.full_result["retryable"] is False
    assert response.context_meta["strategy"] == "transport_error"


def test_pre_dispatch_third_party_write_failure_remains_safely_retryable():
    error = McpToolTransportError(
        {"isError": True, "content": [{"type": "text", "text": "Circuit open"}]},
        code="MCP_CIRCUIT_OPEN",
        dispatch_outcome="not_started",
        retryable=True,
    )
    response = _tool_transport_error_response(error, "write")
    assert response.full_result["outcome"] == "not_started"
    assert response.full_result["retryable"] is True
