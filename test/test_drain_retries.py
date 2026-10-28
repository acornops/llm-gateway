"""Only trusted pre-admission rejection permits replaying a builtin dispatch."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.config.settings import settings
from app.execution_capacity import ExecutionAuthority, execution_authority

DRAIN = {
    "error": {
        "code": "SERVICE_DRAINING",
        "message": "Service is draining",
        "retryable": True,
        "outcome": "not_started",
    }
}
OK = {"status": "ok", "contractVersion": 1, "capacityEnabled": True}
HEADERS = {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "1"}


def install_peer(monkeypatch, peer):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(peer), **kw)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["authorize", "operations/finish", "operations/begin"])
async def test_authority_retries_trusted_drain_with_same_payload(monkeypatch, action):
    requests = []

    def peer(request):
        requests.append(request)
        return (
            httpx.Response(503, json=DRAIN) if len(requests) == 1 else httpx.Response(200, json=OK)
        )

    install_peer(monkeypatch, peer)
    result = await ExecutionAuthority()._post("run", action, {"operationId": "stable"})
    assert result == OK
    assert len(requests) == 2
    assert requests[0].content == requests[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["authorize", "operations/finish"])
async def test_authority_idempotent_calls_retry_lost_response(monkeypatch, action):
    requests = []

    def peer(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(200, json=OK)

    install_peer(monkeypatch, peer)
    assert await ExecutionAuthority()._post("run", action, {}) == OK
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": {**DRAIN["error"], "outcome": "unknown"}}),
        httpx.Response(401, json=DRAIN),
        httpx.Response(307, headers={"location": "http://elsewhere"}, json=DRAIN),
        httpx.Response(503, json={"error": {**DRAIN["error"], "retryable": "true"}}),
        httpx.Response(503, json={"error": {**DRAIN["error"], "code": "UNAVAILABLE"}}),
    ],
)
async def test_begin_never_retries_untrusted_failure(monkeypatch, response):
    requests = []

    def peer(request):
        requests.append(request)
        return response

    install_peer(monkeypatch, peer)
    with pytest.raises(HTTPException):
        await ExecutionAuthority()._post("run", "operations/begin", {})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_lost_begin_response_is_fenced_and_settled(monkeypatch):
    from app.execution_capacity import begin_dispatch

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", True)
    events = []

    async def post(run, action, body):
        events.append((action, dict(body)))
        if action == "operations/begin":
            raise HTTPException(503, "ambiguous")
        return OK

    monkeypatch.setattr(execution_authority, "_post", post)
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"), HEADERS, 1000
    ):
        with pytest.raises(HTTPException):
            await begin_dispatch()
        with pytest.raises(HTTPException, match="replayed"):
            await begin_dispatch()
    assert [a for a, _ in events].count("operations/begin") == 1
    assert events[-1][0] == "operations/finish"
    assert events[-1][1]["operationId"] == events[-2][1]["operationId"]


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [False, True])
async def test_builtin_drain_retries_inside_single_operation_context(monkeypatch, capacity):
    from app.internal_transport import post_builtin_mcp_tool

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", capacity)
    events = []
    requests = []

    async def post(run, action, body):
        events.append((action, dict(body)))
        return {**OK, "capacityEnabled": capacity}

    def peer(request):
        requests.append(request)
        events.append(("dispatch", json.loads(request.content)))
        return (
            httpx.Response(503, json=DRAIN)
            if len(requests) == 1
            else httpx.Response(200, json={"isError": False})
        )

    install_peer(monkeypatch, peer)
    monkeypatch.setattr(execution_authority, "_post", post)
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"), HEADERS, 5000
    ):
        result = await post_builtin_mcp_tool(
            "http://cp/internal/v1/mcp", "write", {}, 1000, {}, "logical-call"
        )
    assert result == {"isError": False}
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    if capacity:
        assert [a for a, _ in events] == [
            "authorize",
            "authorize",
            "operations/begin",
            "dispatch",
            "operations/finish",
            "authorize",
            "operations/begin",
            "dispatch",
            "operations/finish",
        ]
        begins = [b["operationId"] for a, b in events if a == "operations/begin"]
        finishes = [b["operationId"] for a, b in events if a == "operations/finish"]
        assert len(set(begins)) == 2
        assert begins == finishes


@pytest.mark.asyncio
async def test_failed_finish_never_unlocks_builtin_replay(monkeypatch):
    from app.internal_transport import post_builtin_mcp_tool

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", True)
    requests = []

    async def post(run, action, body):
        if action == "operations/finish":
            return {"status": "bad", "contractVersion": 1}
        return OK

    def peer(request):
        requests.append(request)
        return httpx.Response(503, json=DRAIN)

    install_peer(monkeypatch, peer)
    monkeypatch.setattr(execution_authority, "_post", post)
    with pytest.raises(HTTPException):
        async with execution_authority.operation(
            SimpleNamespace(run_id="r", workspace_id="w"), HEADERS, 5000
        ):
            await post_builtin_mcp_tool(
                "http://cp/internal/v1/mcp", "write", {}, 1000, {}, "logical-call"
            )
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_real_write_handler_claims_signed_receipt_once_across_drain_retry(monkeypatch):
    from unittest.mock import AsyncMock

    from cryptography.hazmat.primitives.asymmetric import rsa
    from test_approval_receipts import _receipt
    from test_tool_call import (
        EXAMPLE_TOOL_ALIAS,
        build_token_claims,
        build_tool_call_payload,
        enabled_server,
        reviewed_tool,
    )
    from test_tool_result_normalization import OUTPUT_SCHEMA, producer_result

    import app.api.handlers_tool_call as handler
    from app.api.tool_call_contract import ToolCallRequest
    from app.auth.claims import TokenClaims
    from app.auth.jwt_validator import TokenContext

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", True)
    tool = reviewed_tool(
        capability="write",
        source="builtin",
        mcp_server_url=settings.BUILTIN_TARGET_MCP_SERVER_URL,
        output_schema=OUTPUT_SCHEMA,
        artifact_policy="never",
    )
    server = enabled_server(
        provenance_type="builtin", server_url=settings.BUILTIN_TARGET_MCP_SERVER_URL
    )
    monkeypatch.setattr(handler.tool_registry, "get_tool", AsyncMock(return_value=tool))
    monkeypatch.setattr(handler.mcp_server_registry, "get_server", AsyncMock(return_value=server))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        "app.mcp.approval_receipts.jwks_manager.get_signing_key",
        AsyncMock(return_value=key.public_key()),
    )
    claims = []

    async def claim(*args, **kwargs):
        claims.append(args)
        return len(claims) == 1

    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)
    request = ToolCallRequest(**build_tool_call_payload(tool_call_id="logical-call"))
    request.approval_receipt = _receipt(key, request)
    token = TokenContext(
        claims=TokenClaims(
            **build_token_claims(
                permissions={"allowed_tool_operations": {EXAMPLE_TOOL_ALIAS: "write"}}
            )
        ),
        token="run-token",
    )
    requests = []

    def peer(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json=DRAIN)
        return httpx.Response(
            200,
            json=producer_result(
                {
                    "schemaVersion": "acornops.model-context.v1",
                    "tool": "get_weather",
                    "status": "success",
                    "summary": "Write completed.",
                    "data": {},
                    "omissions": [],
                },
                {"completed": True},
                artifact_policy="never",
            ),
        )

    client_type = httpx.AsyncClient
    install_peer(monkeypatch, peer)
    result = await handler.execute_tool_call(
        request, token, request=SimpleNamespace(headers=HEADERS)
    )
    assert result.is_error is False
    assert len(claims) == 1
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert json.loads(requests[0].content)["toolCallId"] == "logical-call"

    from app.drain import DrainMiddleware, ServiceDrain
    from app.main import app

    drain = ServiceDrain(1)
    drain.start()
    async with client_type(
        transport=httpx.ASGITransport(app=DrainMiddleware(app, drain)), base_url="http://gateway"
    ) as client:
        rejected = await client.post(
            "/api/v1/mcp/tool-call",
            json=request.model_dump(),
            headers={**HEADERS, "Authorization": "Bearer run-token"},
        )
    assert rejected.status_code == 503
    assert rejected.json()["error"]["outcome"] == "not_started"
    assert len(claims) == 1
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_authority_retry_budget_is_total_and_bounded(monkeypatch):
    import time

    import app.execution_capacity as capacity

    monkeypatch.setattr(capacity, "AUTHORITY_RETRY_BUDGET_SECONDS", 0.15)
    calls = []

    def peer(request):
        calls.append(request)
        raise httpx.ReadError("offline", request=request)

    install_peer(monkeypatch, peer)
    started = time.monotonic()
    with pytest.raises(HTTPException):
        await ExecutionAuthority()._post("run", "authorize", {})
    assert time.monotonic() - started < 0.5
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_operation_begin_does_not_retry_a_lost_response(monkeypatch):
    calls = []

    def peer(request):
        calls.append(request)
        raise httpx.ReadError("response lost", request=request)

    install_peer(monkeypatch, peer)
    with pytest.raises(HTTPException):
        await ExecutionAuthority()._post("run", "operations/begin", {})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_builtin_does_not_retry_ambiguous_dispatch(monkeypatch):
    from app.internal_transport import post_builtin_mcp_tool

    calls = []

    def peer(request):
        calls.append(request)
        raise httpx.ReadError("response lost", request=request)

    install_peer(monkeypatch, peer)
    with pytest.raises(httpx.ReadError):
        await post_builtin_mcp_tool(
            "http://cp/internal/v1/mcp", "write", {}, 1000, {}, "logical-call"
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_pause_stops_at_shutdown_deadline(monkeypatch):
    import app.drain as drain_module

    drain = drain_module.ServiceDrain(0.01)
    drain.start()
    monkeypatch.setattr(drain_module, "service_drain", drain)
    assert await drain_module.wait_retry(0.1, drain.deadline + 100) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_enabled", [False, True])
async def test_builtin_http_drain_then_owner_handoff_preserves_tool_window(
    monkeypatch, capacity_enabled
):
    import asyncio
    from unittest.mock import AsyncMock

    from test_tool_call import (
        build_token_claims,
        build_tool_call_payload,
        enabled_server,
        reviewed_tool,
    )
    from test_tool_result_normalization import OUTPUT_SCHEMA, producer_result

    import app.api.handlers_tool_call as handler
    import app.internal_transport as transport
    from app.api.tool_call_contract import ToolCallRequest
    from app.auth.claims import TokenClaims
    from app.auth.jwt_validator import TokenContext

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", capacity_enabled)
    monkeypatch.setattr(settings, "AGENT_HANDOFF_TIMEOUT_MS", 7000)
    # The old implementation derives its retry budget from the handoff setting.
    # The fixed implementation gives that HTTP phase an independent budget.
    monkeypatch.setattr(transport, "BUILTIN_DRAIN_RETRY_TIMEOUT_SECONDS", 7, raising=False)
    tool = reviewed_tool(
        source="builtin",
        timeout_ms=1000,
        output_schema=OUTPUT_SCHEMA,
        artifact_policy="never",
        mcp_server_url=settings.BUILTIN_TARGET_MCP_SERVER_URL,
    )
    server = enabled_server(
        provenance_type="builtin", server_url=settings.BUILTIN_TARGET_MCP_SERVER_URL
    )
    monkeypatch.setattr(handler.tool_registry, "get_tool", AsyncMock(return_value=tool))
    monkeypatch.setattr(handler.mcp_server_registry, "get_server", AsyncMock(return_value=server))
    operations = []

    async def post(run, action, body):
        operations.append((action, dict(body)))
        return {**OK, "capacityEnabled": capacity_enabled}

    monkeypatch.setattr(execution_authority, "_post", post)
    requests = []
    execution_completed = False

    async def peer(request):
        nonlocal execution_completed
        requests.append(request)
        if len(requests) <= 6:
            return httpx.Response(503, json=DRAIN)
        # CP accepts the seventh HTTP request, then starts its own handoff budget.
        await asyncio.sleep(6.5)
        await asyncio.sleep(0.9)  # Near the unchanged one-second tool execution policy.
        execution_completed = True
        return httpx.Response(
            200,
            json=producer_result(
                {
                    "schemaVersion": "acornops.model-context.v1",
                    "tool": "get_weather",
                    "status": "success",
                    "summary": "Completed.",
                    "data": {},
                    "omissions": [],
                },
                {"completed": True},
                artifact_policy="never",
            ),
        )

    install_peer(monkeypatch, peer)
    request = ToolCallRequest(**build_tool_call_payload(tool_call_id="logical-call"))
    token = TokenContext(claims=TokenClaims(**build_token_claims()), token="run-token")
    result = await handler.execute_tool_call(
        request, token, request=SimpleNamespace(headers=HEADERS)
    )
    assert execution_completed, "HTTP retry and owner handoff must not truncate tool execution"
    assert result.is_error is False
    assert len(requests) == 7
    assert len({r.content for r in requests}) == 1
    assert tool.timeout_ms == 1000
    assert requests[-1].extensions["timeout"]["read"] == 13
    begins = [b["operationId"] for a, b in operations if a == "operations/begin"]
    finishes = [b["operationId"] for a, b in operations if a == "operations/finish"]
    assert begins == finishes
    assert len(set(begins)) == (7 if capacity_enabled else 0)
