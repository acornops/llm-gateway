import asyncio
import json
from collections import Counter
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.mcp.egress_policy import ValidatedMcpRequestTarget
from app.mcp.transports.http_transport import (
    McpHttpTransport,
    McpToolTransportError,
)
from app.resilience.outbound import dependency_circuit_breaker


class StrictStreamableMcpServer:
    """Small strict MCP peer used to verify the client lifecycle and headers."""

    def __init__(
        self,
        *,
        use_sse: bool = False,
        protocol_version: str = "2025-11-25",
        issue_session: bool = True,
    ) -> None:
        self.use_sse = use_sse
        self.protocol_version = protocol_version
        self.issue_session = issue_session
        self.requests: list[httpx.Request] = []
        self.initialized_sessions: set[str] = set()
        self.terminated_sessions: set[str] = set()
        self.session_counter = 0
        self.expire_next_operation = False
        self.expire_method: str | None = None

    @staticmethod
    def _body(request: httpx.Request) -> dict[str, object]:
        return json.loads(request.content) if request.content else {}

    def _jsonrpc_response(
        self,
        request_id: object,
        result: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> httpx.Response:
        payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        headers = {"content-type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id
        if not self.use_sse:
            return httpx.Response(200, headers=headers, json=payload)
        headers["content-type"] = "text/event-stream"
        content = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
        return httpx.Response(200, headers=headers, content=content)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/mcp"
        assert request.headers["accept-encoding"] == "identity"

        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            session_id = request.headers.get("mcp-session-id")
            if session_id:
                self.terminated_sessions.add(session_id)
            return httpx.Response(204)

        body = self._body(request)
        method = body.get("method")
        assert "application/json" in request.headers["accept"]
        assert "text/event-stream" in request.headers["accept"]

        if method == "initialize":
            assert "mcp-session-id" not in request.headers
            assert "mcp-protocol-version" not in request.headers
            self.session_counter += 1
            session_id = (
                f"session-{self.session_counter}" if self.issue_session else None
            )
            return self._jsonrpc_response(
                body["id"],
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "strict-test-server", "version": "1.0.0"},
                },
                session_id=session_id,
            )

        session_id = request.headers.get("mcp-session-id")
        if self.issue_session:
            assert session_id is not None
        else:
            assert session_id is None
        assert request.headers["mcp-protocol-version"] == self.protocol_version
        session_key = session_id or "stateless"
        if method == "notifications/initialized":
            self.initialized_sessions.add(session_key)
            return httpx.Response(202)
        assert session_key in self.initialized_sessions

        if self.expire_next_operation or self.expire_method == method:
            self.expire_next_operation = False
            self.expire_method = None
            return httpx.Response(404)
        if method == "tools/list":
            cursor = (body.get("params") or {}).get("cursor")
            if cursor is None:
                return self._jsonrpc_response(
                    body["id"],
                    {
                        "tools": [
                            {
                                "name": "weather.lookup",
                                "description": "Look up weather",
                                "inputSchema": {"type": "object"},
                            }
                        ],
                        "nextCursor": "page-2",
                    },
                )
            assert cursor == "page-2"
            return self._jsonrpc_response(
                body["id"],
                {
                    "tools": [
                        {
                            "name": "weather.alerts",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            )
        if method == "tools/call":
            params = body["params"]
            assert params["name"] == "weather.lookup"
            return self._jsonrpc_response(
                body["id"],
                {
                    "content": [{"type": "text", "text": "Sunny"}],
                    "structuredContent": {"temperature": 22},
                    "isError": False,
                },
            )
        raise AssertionError(f"Unexpected MCP method: {method}")


def transport_for(server: StrictStreamableMcpServer) -> McpHttpTransport:
    return McpHttpTransport(lambda: httpx.MockTransport(server))


@pytest.mark.anyio
async def test_discovers_paginated_tools_with_full_streamable_http_lifecycle() -> None:
    server = StrictStreamableMcpServer()

    payload = await transport_for(server).list_tools("http://mcp.example/mcp", 1000)

    assert [tool["name"] for tool in payload["tools"]] == [
        "weather.lookup",
        "weather.alerts",
    ]
    methods = [
        StrictStreamableMcpServer._body(request).get("method")
        for request in server.requests
        if request.method == "POST"
    ]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
    ]
    assert server.initialized_sessions == {"session-1"}
    assert server.terminated_sessions == {"session-1"}


@pytest.mark.anyio
async def test_calls_tool_and_normalizes_standard_result_aliases() -> None:
    server = StrictStreamableMcpServer()

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp",
        "weather.lookup",
        {"location": "Singapore"},
        1000,
    )

    assert payload == {
        "content": [{"type": "text", "text": "Sunny"}],
        "structuredContent": {"temperature": 22},
        "isError": False,
    }
    assert server.terminated_sessions == {"session-1"}


@pytest.mark.anyio
async def test_transport_owned_headers_cannot_be_overridden() -> None:
    server = StrictStreamableMcpServer()

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp",
        "weather.lookup",
        {},
        1000,
        headers={
            "Accept": "text/plain",
            "MCP-Protocol-Version": "2099-01-01",
            "MCP-Session-Id": "spoofed",
        },
    )

    assert payload["isError"] is False


@pytest.mark.anyio
async def test_rejects_an_unreadable_mcp_specific_ca_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.MCP_EGRESS_CA_BUNDLE_FILE",
        "/missing/acornops-mcp-ca.pem",
    )

    payload = await McpHttpTransport().call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert isinstance(payload, McpToolTransportError)
    assert payload.code == "MCP_EGRESS_BLOCKED"
    assert payload.dispatch_outcome == "not_started"


@pytest.mark.anyio
async def test_supports_a_stateless_streamable_http_server() -> None:
    server = StrictStreamableMcpServer(issue_session=False)

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert payload["isError"] is False
    assert server.initialized_sessions == {"stateless"}
    assert server.terminated_sessions == set()
    assert all(
        "mcp-session-id" not in request.headers for request in server.requests
    )


@pytest.mark.anyio
async def test_supports_sse_initialize_discovery_and_tool_responses() -> None:
    discovery_server = StrictStreamableMcpServer(use_sse=True)
    call_server = StrictStreamableMcpServer(use_sse=True)

    discovered = await transport_for(discovery_server).list_tools(
        "http://mcp.example/mcp", 1000
    )
    called = await transport_for(call_server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert len(discovered["tools"]) == 2
    assert called["structuredContent"] == {"temperature": 22}


@pytest.mark.anyio
async def test_rejects_an_unsupported_negotiated_protocol_version() -> None:
    server = StrictStreamableMcpServer(protocol_version="2099-01-01")

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert isinstance(payload, McpToolTransportError)
    assert payload.code == "MCP_PROTOCOL_ERROR"
    assert payload["content"][0]["text"] == "MCP protocol error"


@pytest.mark.anyio
async def test_uses_a_supported_server_negotiated_protocol_version() -> None:
    server = StrictStreamableMcpServer(protocol_version="2025-06-18")

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert payload["isError"] is False
    operation = next(
        request
        for request in server.requests
        if request.method == "POST"
        and StrictStreamableMcpServer._body(request).get("method") == "tools/call"
    )
    assert operation.headers["mcp-protocol-version"] == "2025-06-18"


@pytest.mark.anyio
async def test_reinitializes_discovery_once_after_explicit_session_termination() -> None:
    server = StrictStreamableMcpServer()
    server.expire_next_operation = True

    payload = await transport_for(server).list_tools(
        "http://mcp.example/mcp", 1000
    )

    assert len(payload["tools"]) == 2
    assert server.session_counter == 2
    assert server.initialized_sessions == {"session-1", "session-2"}


@pytest.mark.anyio
async def test_never_replays_a_tool_after_session_termination() -> None:
    server = StrictStreamableMcpServer()
    server.expire_method = "tools/call"

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert isinstance(payload, McpToolTransportError)
    assert payload.dispatch_outcome == "unknown"
    assert server.session_counter == 1
    tool_calls = [
        request
        for request in server.requests
        if request.method == "POST"
        and StrictStreamableMcpServer._body(request).get("method") == "tools/call"
    ]
    assert len(tool_calls) == 1


@pytest.mark.anyio
async def test_concurrent_operations_never_share_mcp_sessions() -> None:
    server = StrictStreamableMcpServer()
    transport = transport_for(server)

    first, second = await asyncio.gather(
        transport.call_tool(
            "http://mcp.example/mcp", "weather.lookup", {"request": 1}, 1000
        ),
        transport.call_tool(
            "http://mcp.example/mcp", "weather.lookup", {"request": 2}, 1000
        ),
    )

    assert first["isError"] is False
    assert second["isError"] is False
    assert server.initialized_sessions == {"session-1", "session-2"}
    assert server.terminated_sessions == {"session-1", "session-2"}
    operation_sessions = [
        request.headers["mcp-session-id"]
        for request in server.requests
        if request.method == "POST"
        and StrictStreamableMcpServer._body(request).get("method") == "tools/call"
    ]
    assert Counter(operation_sessions) == Counter({"session-1": 1, "session-2": 1})


@pytest.mark.anyio
async def test_uses_pinned_connection_target_and_original_host_for_all_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = StrictStreamableMcpServer()
    target = ValidatedMcpRequestTarget(
        original_url="https://mcp.example/mcp",
        connection_url="https://93.184.216.34/mcp",
        host_header="mcp.example",
        extensions={"sni_hostname": "mcp.example"},
    )
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.prepare_mcp_egress_request",
        AsyncMock(return_value=target),
    )

    payload = await transport_for(server).call_tool(
        target.original_url, "weather.lookup", {}, 1000
    )

    assert payload["isError"] is False
    assert all(request.url.host == "93.184.216.34" for request in server.requests)
    assert all(request.headers["host"] == "mcp.example" for request in server.requests)
    assert all(
        request.extensions.get("sni_hostname") == "mcp.example"
        for request in server.requests
    )


@pytest.mark.anyio
async def test_rejects_oversized_streamable_http_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = StrictStreamableMcpServer()
    original_response = server._jsonrpc_response

    def oversized_response(
        request_id: object,
        result: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> httpx.Response:
        if "structuredContent" in result:
            result = {
                **result,
                "structuredContent": {"value": "x" * 2048},
            }
        return original_response(request_id, result, session_id=session_id)

    server._jsonrpc_response = oversized_response  # type: ignore[method-assign]
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.MCP_MAX_TOOL_RESULT_BYTES", 1024
    )

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert isinstance(payload, McpToolTransportError)
    assert payload.code == "MCP_RESULT_TOO_LARGE"
    assert payload["content"][0]["text"] == "MCP response exceeds the result limit"


@pytest.mark.anyio
async def test_rejects_compressed_mcp_responses() -> None:
    server = StrictStreamableMcpServer()
    original_response = server._jsonrpc_response

    def compressed_response(
        request_id: object,
        result: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> httpx.Response:
        response = original_response(request_id, result, session_id=session_id)
        if "structuredContent" in result:
            response.headers["content-encoding"] = "gzip"
        return response

    server._jsonrpc_response = compressed_response  # type: ignore[method-assign]

    payload = await transport_for(server).call_tool(
        "http://mcp.example/mcp", "weather.lookup", {}, 1000
    )

    assert isinstance(payload, McpToolTransportError)
    assert payload.code == "MCP_PROTOCOL_ERROR"
    assert payload["content"][0]["text"] == "MCP protocol error"


@pytest.mark.anyio
async def test_logs_only_bounded_sanitized_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = McpHttpTransport()
    log = Mock()
    monkeypatch.setattr("app.mcp.transports.http_transport.logger", log)
    request = httpx.Request(
        "POST",
        "https://mcp.example/mcp",
        headers={
            "Authorization": "Bearer top-secret-token",
            "X-Short-Secret": "tiny",
        },
    )
    response = httpx.Response(
        400,
        request=request,
        json={
            "jsonrpc": "2.0",
            "error": {
                "code": -32600,
                "message": (
                    "Bad Request for Bearer top-secret-token and tiny: Missing session ID"
                ),
            },
        },
    )

    await transport._observe_response(response)

    fields = log.warning.call_args.kwargs
    assert fields["status_code"] == 400
    assert fields["upstream_error"] == (
        "Bad Request for [REDACTED] and [REDACTED]: Missing session ID"
    )
    assert "top-secret-token" not in str(log.warning.call_args)
    assert "tiny" not in str(log.warning.call_args)


@pytest.mark.anyio
async def test_egress_logs_never_include_url_credentials_or_query_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Mock()
    monkeypatch.setattr("app.mcp.transports.http_transport.logger", log)

    payload = await McpHttpTransport().call_tool(
        "https://user:password@mcp.example/mcp?token=query-secret",
        "weather.lookup",
        {},
        1000,
    )

    assert isinstance(payload, McpToolTransportError)
    fields = log.warning.call_args.kwargs
    assert fields["server_url"] == "https://mcp.example"
    assert "password" not in str(log.warning.call_args)
    assert "query-secret" not in str(log.warning.call_args)


@pytest.mark.anyio
async def test_discovery_retries_transient_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = StrictStreamableMcpServer()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        body = StrictStreamableMcpServer._body(request)
        if body.get("method") == "initialize":
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectTimeout("temporary timeout", request=request)
        return server(request)

    await dependency_circuit_breaker.reset()
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.MCP_DISCOVERY_RETRY_BACKOFF_MS", 1
    )
    transport = McpHttpTransport(lambda: httpx.MockTransport(handler))

    try:
        payload = await transport.list_tools("http://mcp.example/mcp", 1000)
    finally:
        await dependency_circuit_breaker.reset()

    assert len(payload["tools"]) == 2
    assert attempts == 3


@pytest.mark.anyio
async def test_tool_calls_open_the_circuit_after_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("connection failed", request=request)

    await dependency_circuit_breaker.reset()
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        2,
    )
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS",
        60_000,
    )
    transport = McpHttpTransport(lambda: httpx.MockTransport(handler))

    try:
        first = await transport.call_tool(
            "http://mcp.example/mcp", "weather.lookup", {}, 1000
        )
        second = await transport.call_tool(
            "http://mcp.example/mcp", "weather.lookup", {}, 1000
        )
        third = await transport.call_tool(
            "http://mcp.example/mcp", "weather.lookup", {}, 1000
        )
    finally:
        await dependency_circuit_breaker.reset()

    assert isinstance(first, McpToolTransportError)
    assert isinstance(second, McpToolTransportError)
    assert isinstance(third, McpToolTransportError)
    assert first.code == "MCP_TOOL_REQUEST_FAILED"
    assert second.code == "MCP_TOOL_REQUEST_FAILED"
    assert third.code == "MCP_CIRCUIT_OPEN"
    assert len(requests) == 2
