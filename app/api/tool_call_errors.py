from typing import Any

import httpx

from app.api.tool_result_normalization import ToolCallResponse, _json_size
from app.errors.codes import ErrorCode
from app.mcp.transports.http_transport import McpToolTransportError
from app.secrets.errors import SecretNotFoundError


def _is_missing_secret_error(exc: Exception) -> bool:
    return isinstance(exc, SecretNotFoundError)


def _tool_execution_error_response(exc: Exception, capability: str) -> ToolCallResponse:
    """Return a bounded error and conservatively mark ambiguous write outcomes."""
    is_timeout = isinstance(exc, httpx.TimeoutException)
    write_capable = capability != "read"
    error: dict[str, Any] = {
        "code": ErrorCode.TOOL_TIMEOUT if is_timeout else ErrorCode.TOOL_EXECUTION_FAILED,
        "message": "Tool execution timed out" if is_timeout else "Tool execution failed",
        "retryable": is_timeout and not write_capable,
    }
    if write_capable:
        error["outcome"] = "unknown"
    size = _json_size(error)
    return ToolCallResponse(
        full_result=error,
        model_context=error,
        context_meta={
            "schema_version": "v1",
            "strategy": "error",
            "original_bytes": size,
            "context_bytes": size,
            "truncated": False,
            "omissions": [],
        },
        artifact_eligible=False,
        is_error=True,
    )


def _tool_transport_error_response(
    error: McpToolTransportError, capability: str
) -> ToolCallResponse:
    """Preserve trusted local MCP dispatch state for safe write retry behavior."""
    write_capable = capability != "read"
    result: dict[str, Any] = {
        "code": error.code,
        "message": str(error.get("content", [{}])[0].get("text") or "MCP tool call failed."),
        "retryable": error.retryable
        and (not write_capable or error.dispatch_outcome == "not_started"),
    }
    if write_capable:
        result["outcome"] = error.dispatch_outcome
    size = _json_size(result)
    return ToolCallResponse(
        full_result=result,
        model_context=result,
        context_meta={
            "schema_version": "v1",
            "strategy": "transport_error",
            "original_bytes": size,
            "context_bytes": size,
            "truncated": False,
            "omissions": [],
        },
        artifact_eligible=False,
        is_error=True,
    )


def _mark_unknown_write_contract(response: ToolCallResponse, capability: str) -> ToolCallResponse:
    """A malformed response cannot prove that a dispatched write did not happen."""
    if (
        capability != "read"
        and response.is_error
        and isinstance(response.full_result, dict)
        and response.full_result.get("code") in {
            "TOOL_RESULT_SCHEMA_INVALID",
            "TOOL_RESULT_CONTRACT_INVALID",
        }
    ):
        error = {**response.full_result, "outcome": "unknown", "retryable": False}
        size = _json_size(error)
        response.full_result = error
        response.model_context = error
        response.context_meta = {
            **response.context_meta,
            "original_bytes": size,
            "context_bytes": size,
        }
    return response
