import asyncio
from typing import Any

import httpx
import structlog

from app.config.settings import settings
from app.mcp.egress_policy import (
    McpEgressPolicyError,
    ValidatedMcpRequestTarget,
    prepare_mcp_egress_request,
)
from app.resilience.outbound import (
    CircuitOpenError,
    backoff_seconds,
    dependency_circuit_breaker,
    is_retryable_dependency_error,
    note_dependency_event,
)

logger = structlog.get_logger()


class McpToolTransportError(dict[str, Any]):
    """Sanitized MCP error carrying trusted local dispatch semantics."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        code: str,
        dispatch_outcome: str,
        retryable: bool,
    ) -> None:
        super().__init__(payload)
        self.code = code
        self.dispatch_outcome = dispatch_outcome
        self.retryable = retryable


class McpHttpTransport:
    """
    Handles tool execution over HTTP/SSE Model Context Protocol.
    Uses connection pooling for performance.
    """

    def __init__(self):
        self._client = httpx.AsyncClient()

    @staticmethod
    def _error_payload(prefix: str, detail: str | None = None) -> dict[str, Any]:
        message = f"{prefix}: {detail}" if detail else prefix
        return {
            "isError": True,
            "content": [{"type": "text", "text": message}],
        }

    @classmethod
    def _transport_error_payload(
        cls,
        prefix: str,
        *,
        code: str,
        dispatch_outcome: str,
        retryable: bool,
        detail: str | None = None,
    ) -> McpToolTransportError:
        return McpToolTransportError(
            cls._error_payload(prefix, detail),
            code=code,
            dispatch_outcome=dispatch_outcome,
            retryable=retryable,
        )

    async def _request_bounded(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Read at most the decoded MCP result ceiling, closing oversized streams early."""
        async with self._client.stream(method, url, **kwargs) as response:
            if response.status_code >= 400:
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=b"",
                    request=response.request,
                )
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > settings.MCP_MAX_TOOL_RESULT_BYTES:
                    raise ValueError("MCP response exceeds the 2 MiB result limit")
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=response.request,
            )

    async def close(self):
        """Closes the underlying HTTP client."""
        await self._client.aclose()

    @staticmethod
    def _request_headers(
        headers: dict[str, str] | None,
        target: ValidatedMcpRequestTarget,
    ) -> dict[str, str]:
        request_headers = dict(headers or {})
        request_headers["host"] = target.host_header
        return request_headers

    async def call_tool(
        self,
        url: str,
        name: str,
        arguments: dict[str, Any],
        timeout_ms: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Executes a tool call on a remote MCP server.
        """
        try:
            target = await prepare_mcp_egress_request(url)
            dependency_key = f"mcp:call:{url.rstrip('/')}"
            await dependency_circuit_breaker.before_call(dependency_key, "mcp", url)
            payload = await self._call_tool_once(
                target,
                name,
                arguments,
                timeout_ms,
                self._request_headers(headers, target),
            )
            await dependency_circuit_breaker.record_success(dependency_key)
            return payload
        except CircuitOpenError as exc:
            logger.warning("mcp_tool_call_circuit_open", server_url=url, tool=name, error=str(exc))
            return self._transport_error_payload(
                "MCP server unavailable",
                code="MCP_CIRCUIT_OPEN",
                dispatch_outcome="not_started",
                retryable=True,
            )
        except McpEgressPolicyError as exc:
            logger.warning(
                "mcp_tool_call_egress_policy_failed",
                server_url=url,
                tool=name,
                error=str(exc),
            )
            return self._transport_error_payload(
                "MCP server blocked by egress policy",
                code="MCP_EGRESS_BLOCKED",
                dispatch_outcome="not_started",
                retryable=False,
            )
        except Exception as exc:
            logger.warning("mcp_tool_call_failed", server_url=url, tool=name, error=str(exc))
            note_dependency_event("mcp", "failure")
            if is_retryable_dependency_error(exc):
                opened = await dependency_circuit_breaker.record_failure(
                    f"mcp:call:{url.rstrip('/')}",
                    settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                    settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
                )
                if opened:
                    note_dependency_event("mcp", "circuit_open")
            if isinstance(exc, httpx.TimeoutException):
                code = "MCP_TOOL_TIMEOUT"
            elif isinstance(exc, httpx.RequestError):
                code = "MCP_TOOL_REQUEST_FAILED"
            else:
                code = "MCP_TOOL_TRANSPORT_FAILED"
            return self._transport_error_payload(
                self._tool_error_prefix(exc),
                code=code,
                dispatch_outcome="unknown",
                retryable=isinstance(exc, (httpx.TimeoutException, httpx.RequestError)),
            )

    async def list_tools(
        self,
        url: str,
        timeout_ms: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Best-effort remote tool discovery for MCP servers."""
        try:
            target = await prepare_mcp_egress_request(url)
        except Exception as exc:
            logger.warning(
                "mcp_tool_discovery_egress_policy_failed",
                server_url=url,
                error=str(exc),
            )
            return self._error_payload("MCP server blocked by egress policy")

        dependency_key = f"mcp:discovery:{url.rstrip('/')}"
        attempts = max(1, settings.MCP_DISCOVERY_RETRY_ATTEMPTS)
        attempt = 1
        try:
            while attempt <= attempts:
                await dependency_circuit_breaker.before_call(dependency_key, "mcp", url)
                try:
                    payload = await self._list_tools_once(
                        target,
                        timeout_ms,
                        self._request_headers(headers, target),
                    )
                    await dependency_circuit_breaker.record_success(dependency_key)
                    return payload
                except Exception as exc:
                    logger.warning(
                        "mcp_tool_discovery_attempt_failed",
                        server_url=url,
                        attempt=attempt,
                        max_attempts=attempts,
                        error=str(exc),
                    )
                    note_dependency_event("mcp", "failure")
                    retryable = is_retryable_dependency_error(exc)
                    if retryable:
                        opened = await dependency_circuit_breaker.record_failure(
                            dependency_key,
                            settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                            settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
                        )
                        if opened:
                            note_dependency_event("mcp", "circuit_open")
                        if attempt < attempts and not opened:
                            note_dependency_event("mcp", "retry")
                            await asyncio.sleep(
                                backoff_seconds(settings.MCP_DISCOVERY_RETRY_BACKOFF_MS, attempt)
                            )
                            attempt += 1
                            continue
                    return self._error_payload(self._discovery_error_prefix(exc))
        except CircuitOpenError as exc:
            logger.warning("mcp_tool_discovery_circuit_open", server_url=url, error=str(exc))
            return self._error_payload("MCP server unavailable")
        except Exception as exc:
            logger.warning("mcp_tool_discovery_failed", server_url=url, error=str(exc))
            return self._error_payload(self._discovery_error_prefix(exc))

    def _tool_error_prefix(self, exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "MCP server timeout"
        if isinstance(exc, httpx.RequestError):
            return "Failed to connect to MCP server"
        return "Upstream tool error"

    def _discovery_error_prefix(self, exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "MCP server discovery timeout"
        if isinstance(exc, httpx.RequestError):
            return "Failed to connect to MCP server"
        return "Upstream tool discovery error"

    async def _call_tool_once(
        self,
        target: ValidatedMcpRequestTarget,
        name: str,
        arguments: dict[str, Any],
        timeout_ms: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._request_bounded(
            "POST",
            f"{target.connection_url}/tools/call",
            json={"name": name, "arguments": arguments},
            timeout=timeout_ms / 1000.0,
            headers=headers,
            extensions=target.extensions,
            follow_redirects=False,
        )
        if response.status_code in (404, 405):
            # Fallback for JSON-RPC style MCP servers exposed on root.
            response = await self._request_bounded(
                "POST",
                target.connection_url,
                json={
                    "jsonrpc": "2.0",
                    "id": "acornops-tools-call",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                timeout=timeout_ms / 1000.0,
                headers=headers,
                extensions=target.extensions,
                follow_redirects=False,
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            if "error" in payload:
                logger.warning("mcp_jsonrpc_tool_error", server_url=target.original_url, tool=name)
                return self._transport_error_payload(
                    "Upstream tool error",
                    code="MCP_JSONRPC_ERROR",
                    dispatch_outcome="unknown",
                    retryable=False,
                )
            if "result" not in payload or payload["result"] is None:
                return self._transport_error_payload(
                    "Upstream tool error",
                    code="MCP_RESULT_INVALID",
                    dispatch_outcome="unknown",
                    retryable=False,
                    detail="Invalid JSON-RPC tool result payload.",
                )
            result = payload["result"]
            if isinstance(result, dict):
                return result
            return self._transport_error_payload(
                "Upstream tool error",
                code="MCP_RESULT_INVALID",
                dispatch_outcome="unknown",
                retryable=False,
                detail="JSON-RPC tool result must be an object.",
            )
        if not isinstance(payload, dict):
            return self._transport_error_payload(
                "Upstream tool error",
                code="MCP_RESULT_INVALID",
                dispatch_outcome="unknown",
                retryable=False,
                detail="Invalid response payload from MCP server.",
            )
        return payload

    async def _list_tools_once(
        self,
        target: ValidatedMcpRequestTarget,
        timeout_ms: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        timeout_sec = timeout_ms / 1000.0
        response = await self._request_bounded(
            "POST",
            f"{target.connection_url}/tools/list",
            json={},
            timeout=timeout_sec,
            headers=headers,
            extensions=target.extensions,
            follow_redirects=False,
        )
        if response.status_code in (404, 405):
            # Some servers expose discovery on GET /tools/list.
            response = await self._request_bounded(
                "GET",
                f"{target.connection_url}/tools/list",
                timeout=timeout_sec,
                headers=headers,
                extensions=target.extensions,
                follow_redirects=False,
            )
        if response.status_code in (404, 405):
            # Fallback for JSON-RPC style MCP servers exposed on root.
            response = await self._request_bounded(
                "POST",
                target.connection_url,
                json={
                    "jsonrpc": "2.0",
                    "id": "acornops-tools-list",
                    "method": "tools/list",
                    "params": {},
                },
                timeout=timeout_sec,
                headers=headers,
                extensions=target.extensions,
                follow_redirects=False,
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            if "error" in payload:
                logger.warning("mcp_jsonrpc_tool_discovery_error", server_url=target.original_url)
                return self._error_payload(
                    "Upstream tool discovery error",
                )
            result = payload.get("result")
            if isinstance(result, dict):
                return result
            return self._error_payload(
                "Upstream tool discovery error",
                "Invalid JSON-RPC discovery result payload.",
            )
        if not isinstance(payload, dict):
            return self._error_payload(
                "Upstream tool discovery error",
                "Invalid response payload from MCP server tools/list.",
            )
        return payload


mcp_transport = McpHttpTransport()
