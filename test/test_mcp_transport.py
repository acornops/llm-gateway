from unittest.mock import AsyncMock

import httpx
import pytest

from app.mcp.egress_policy import ValidatedMcpRequestTarget
from app.mcp.transports.http_transport import McpHttpTransport
from app.resilience.outbound import dependency_circuit_breaker


@pytest.mark.anyio
async def test_call_tool_falls_back_to_jsonrpc_and_wraps_scalar_result() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.path == "/tools/call":
            return httpx.Response(status_code=404, json={"detail": "missing"})
        return httpx.Response(
            status_code=200,
            json={"jsonrpc": "2.0", "id": "1", "result": "Sunny"},
        )

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
    finally:
        await transport.close()

    assert payload == {
        "isError": False,
        "content": [{"type": "text", "text": "Sunny"}],
    }
    assert requests == [
        ("POST", "http://mcp.example/tools/call"),
        ("POST", "http://mcp.example"),
    ]


@pytest.mark.anyio
async def test_call_tool_returns_error_for_invalid_non_dict_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json=["unexpected"])

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
    finally:
        await transport.close()

    assert payload == {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": "Upstream tool error: Invalid response payload from MCP server.",
            }
        ],
    }


@pytest.mark.anyio
async def test_call_tool_returns_error_for_jsonrpc_payload_missing_result() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"jsonrpc": "2.0", "id": "1"},
        )

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
    finally:
        await transport.close()

    assert payload == {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": "Upstream tool error: Invalid JSON-RPC tool result payload.",
            }
        ],
    }


@pytest.mark.anyio
async def test_call_tool_returns_sanitized_error_for_jsonrpc_error_envelope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "error": {
                    "code": -32603,
                    "message": "internal http://mcp.internal secret failed",
                },
            },
        )

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
    finally:
        await transport.close()

    assert payload == {
        "isError": True,
        "content": [{"type": "text", "text": "Upstream tool error"}],
    }


@pytest.mark.anyio
async def test_call_tool_uses_pinned_connection_target(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    target = ValidatedMcpRequestTarget(
        original_url="https://mcp.example",
        connection_url="https://93.184.216.34",
        host_header="mcp.example",
        extensions={"sni_hostname": "mcp.example"},
    )
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.prepare_mcp_egress_request",
        AsyncMock(return_value=target),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code=200, json={"content": [], "isError": False})

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.call_tool(
            "https://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
            {"x-custom": "true"},
        )
    finally:
        await transport.close()

    assert payload == {"content": [], "isError": False}
    assert str(requests[0].url) == "https://93.184.216.34/tools/call"
    assert requests[0].headers["host"] == "mcp.example"
    assert requests[0].extensions["sni_hostname"] == "mcp.example"


@pytest.mark.anyio
async def test_list_tools_falls_back_to_jsonrpc_and_unwraps_result() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "POST" and request.url.path == "/tools/list":
            return httpx.Response(status_code=404, json={"detail": "missing"})
        if request.method == "GET" and request.url.path == "/tools/list":
            return httpx.Response(status_code=405, json={"detail": "missing"})
        return httpx.Response(
            status_code=200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"tools": [{"name": "weather.lookup"}]},
            },
        )

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.list_tools("http://mcp.example", 1000)
    finally:
        await transport.close()

    assert payload == {"tools": [{"name": "weather.lookup"}]}
    assert requests == [
        ("POST", "http://mcp.example/tools/list"),
        ("GET", "http://mcp.example/tools/list"),
        ("POST", "http://mcp.example"),
    ]


@pytest.mark.anyio
async def test_list_tools_returns_error_for_jsonrpc_error_envelope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "error": {"code": -32601, "message": "Method not found"},
            },
        )

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.list_tools("http://mcp.example", 1000)
    finally:
        await transport.close()

    assert payload == {
        "isError": True,
        "content": [{"type": "text", "text": "Upstream tool discovery error"}],
    }


@pytest.mark.anyio
async def test_list_tools_retries_transient_discovery_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    await dependency_circuit_breaker.reset()
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.MCP_DISCOVERY_RETRY_BACKOFF_MS",
        1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) < 3:
            raise httpx.ConnectTimeout("temporary timeout", request=request)
        return httpx.Response(status_code=200, json={"tools": [{"name": "weather.lookup"}]})

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        payload = await transport.list_tools("http://mcp.example", 1000)
    finally:
        await transport.close()
        await dependency_circuit_breaker.reset()

    assert payload == {"tools": [{"name": "weather.lookup"}]}
    assert requests == [
        "http://mcp.example/tools/list",
        "http://mcp.example/tools/list",
        "http://mcp.example/tools/list",
    ]


@pytest.mark.anyio
async def test_call_tool_opens_circuit_after_repeated_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    await dependency_circuit_breaker.reset()
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        2,
    )
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS",
        60000,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise httpx.ConnectError("connection failed", request=request)

    transport = McpHttpTransport()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        first = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
        second = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
        third = await transport.call_tool(
            "http://mcp.example",
            "weather.lookup",
            {"location": "SF"},
            1000,
        )
    finally:
        await transport.close()
        await dependency_circuit_breaker.reset()

    assert first["content"][0]["text"] == "Failed to connect to MCP server"
    assert second["content"][0]["text"] == "Failed to connect to MCP server"
    assert third["content"][0]["text"] == "MCP server unavailable"
    assert "connection failed" not in first["content"][0]["text"]
    assert requests == [
        "http://mcp.example/tools/call",
        "http://mcp.example/tools/call",
    ]
