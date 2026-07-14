"""Trusted MCP result validation and gateway normalization."""

import json
from typing import Any

from jsonschema import SchemaError as JsonSchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel

from app.observability.metrics import (
    GATEWAY_TOOL_RESULT_BYTES,
    GATEWAY_TOOL_RESULT_NORMALIZATIONS_TOTAL,
)


class ToolCallResponse(BaseModel):
    """Separate complete and model-facing tool result views."""

    full_result: Any
    model_context: Any
    context_meta: dict[str, Any]
    artifact_eligible: bool = False
    is_error: bool


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _parse_model_content(content: Any) -> Any:
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
    ):
        try:
            return json.loads(content[0]["text"])
        except json.JSONDecodeError:
            pass
    return content


def _normalize_tool_response(
    mcp_response: dict[str, Any], *, trusted_builtin: bool,
    output_schema: dict[str, Any] | None = None,
    artifact_policy: str = "never",
    expected_tool: str | None = None,
) -> ToolCallResponse:
    """Separate model evidence from complete MCP structured output."""
    content = mcp_response.get("content", [])
    structured = mcp_response.get("structuredContent")
    metadata = mcp_response.get("_meta") if isinstance(mcp_response.get("_meta"), dict) else {}
    acornops_meta = metadata.get("acornops.dev/result") if isinstance(metadata, dict) else None
    model_context = _parse_model_content(content)
    full_result = structured if structured is not None else content
    strategy = "mcp_content"

    valid_content_shape = "content" in mcp_response and isinstance(content, list) and all(
        isinstance(block, dict) and isinstance(block.get("type"), str)
        for block in content
    )
    valid_structured_shape = structured is None or isinstance(structured, dict)
    valid_error_shape = "isError" not in mcp_response or isinstance(
        mcp_response.get("isError"), bool
    )
    if not trusted_builtin and not all(
        (valid_content_shape, valid_structured_shape, valid_error_shape)
    ):
        GATEWAY_TOOL_RESULT_NORMALIZATIONS_TOTAL.labels(strategy="contract_error").inc()
        error = {
            "code": "TOOL_RESULT_CONTRACT_INVALID",
            "message": "Tool returned an invalid MCP result contract.",
        }
        size = _json_size(error)
        return ToolCallResponse(
            full_result=error,
            model_context=error,
            context_meta={
                "schema_version": "v1", "strategy": "contract_error",
                "original_bytes": _json_size(mcp_response), "context_bytes": size,
                "truncated": False, "omissions": [],
            },
            artifact_eligible=False,
            is_error=True,
        )

    if trusted_builtin:
        declared_is_error = mcp_response.get("isError")
        actual_context_bytes = _json_size(model_context)
        transmitted_context_bytes = (
            len(content[0]["text"].encode("utf-8"))
            if isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and isinstance(content[0].get("text"), str)
            else None
        )
        actual_result_bytes = (
            _json_size(structured["data"])
            if isinstance(structured, dict) and "data" in structured
            else None
        )
        metadata_policy_matches = (
            isinstance(acornops_meta, dict)
            and acornops_meta.get("artifactPolicy") == artifact_policy
            and acornops_meta.get("contextSchemaVersion") == "v1"
        )
        valid_output_schema = False
        if isinstance(output_schema, dict):
            try:
                jsonschema_validate(instance=structured, schema=output_schema)
            except (JsonSchemaValidationError, JsonSchemaError):
                pass
            else:
                valid_output_schema = True
        valid_context = (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and isinstance(content[0].get("text"), str)
            and isinstance(model_context, dict)
            and model_context.get("schemaVersion") == "acornops.model-context.v1"
            and isinstance(model_context.get("tool"), str)
            and (expected_tool is None or model_context.get("tool") == expected_tool)
            and model_context.get("status") in {"success", "error"}
            and isinstance(model_context.get("summary"), str)
            and bool(model_context["summary"])
            and len(model_context["summary"]) <= 500
            and isinstance(model_context.get("data"), dict)
            and isinstance(model_context.get("omissions"), list)
            and actual_context_bytes <= 12 * 1024
            and isinstance(declared_is_error, bool)
            and (model_context.get("status") == "error") == declared_is_error
        )
        valid_full = (
            isinstance(structured, dict)
            and structured.get("schemaVersion") == "acornops.full-tool-result.v1"
            and "data" in structured
        )
        valid_sizes = (
            isinstance(acornops_meta, dict)
            and isinstance(acornops_meta.get("contextBytes"), int)
            and acornops_meta["contextBytes"] == transmitted_context_bytes
            and acornops_meta["contextBytes"] <= 12 * 1024
            and isinstance(acornops_meta.get("originalBytes"), int)
            and acornops_meta["originalBytes"] >= 0
            and acornops_meta["originalBytes"] <= 2 * 1024 * 1024
            and valid_full
            and actual_result_bytes is not None
            and actual_result_bytes <= 2 * 1024 * 1024
        )
        valid_envelope = all(
            (valid_context, valid_full, valid_output_schema, metadata_policy_matches, valid_sizes)
        )
        if not valid_envelope:
            GATEWAY_TOOL_RESULT_NORMALIZATIONS_TOTAL.labels(strategy="schema_error").inc()
            error = {
                "code": "TOOL_RESULT_SCHEMA_INVALID",
                "message": "Trusted tool returned an invalid structured result envelope.",
            }
            return ToolCallResponse(
                full_result=error, model_context=error,
                context_meta={
                    "schema_version": "v1", "strategy": "schema_error",
                    "original_bytes": _json_size(mcp_response),
                    "context_bytes": _json_size(error), "truncated": False, "omissions": [],
                },
                artifact_eligible=False, is_error=True,
            )
        full_result = structured["data"]
        strategy = "producer_projection"

    original_bytes = _json_size(full_result)
    context_bytes = _json_size(model_context)
    omissions = model_context.get("omissions", []) if isinstance(model_context, dict) else []
    GATEWAY_TOOL_RESULT_BYTES.labels(view="full").observe(original_bytes)
    GATEWAY_TOOL_RESULT_BYTES.labels(view="model_context").observe(context_bytes)
    GATEWAY_TOOL_RESULT_NORMALIZATIONS_TOTAL.labels(strategy=strategy).inc()
    return ToolCallResponse(
        full_result=full_result, model_context=model_context,
        context_meta={
            "schema_version": "v1", "strategy": strategy, "original_bytes": original_bytes,
            "context_bytes": context_bytes, "truncated": bool(omissions), "omissions": omissions,
        },
        artifact_eligible=(
            trusted_builtin and not bool(mcp_response.get("isError", False))
            and artifact_policy in {"always", "if_detailed"}
            and (artifact_policy == "always" or original_bytes > context_bytes)
        ),
        is_error=bool(mcp_response.get("isError", False)),
    )
