"""Authority is checked at each upstream dispatch, independent of JWT age."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_disabled_mode_still_denies_suspended_lifecycle(monkeypatch):
    from app.execution_capacity import execution_authority

    monkeypatch.setattr(
        execution_authority, "_post", AsyncMock(side_effect=HTTPException(409, "suspended"))
    )
    calls = []
    with pytest.raises(HTTPException):
        async with execution_authority.operation(
            SimpleNamespace(run_id="r", workspace_id="w"), {}, 1000
        ):
            calls.append("upstream")
    assert calls == []


@pytest.mark.asyncio
async def test_operation_finishes_after_upstream_error(monkeypatch):
    from app.config.settings import settings
    from app.execution_capacity import begin_dispatch, execution_authority

    monkeypatch.setitem(settings.__dict__, "WORKSPACE_CAPACITY_ENABLED", True)
    post = AsyncMock(
        side_effect=[
            {"status": "ok", "capacityEnabled": True, "contractVersion": 1},
            {"status": "ok", "capacityEnabled": True, "contractVersion": 1},
            {"status": "ok", "contractVersion": 1},
            {"status": "ok", "contractVersion": 1},
        ]
    )
    monkeypatch.setattr(execution_authority, "_post", post)
    with pytest.raises(ValueError):
        async with execution_authority.operation(
            SimpleNamespace(run_id="r", workspace_id="w"),
            {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "2"},
            1000,
        ):
            await begin_dispatch()
            raise ValueError("upstream failure")
    assert [c.args[1] for c in post.call_args_list] == [
        "authorize",
        "authorize",
        "operations/begin",
        "operations/finish",
    ]
    assert (
        post.call_args_list[2].args[2]["operationId"]
        == post.call_args_list[3].args[2]["operationId"]
    )


@pytest.mark.asyncio
async def test_missing_owner_makes_zero_upstream_calls(monkeypatch):
    from app.config.settings import settings
    from app.execution_capacity import execution_authority

    monkeypatch.setitem(settings.__dict__, "WORKSPACE_CAPACITY_ENABLED", True)
    monkeypatch.setattr(
        execution_authority,
        "_post",
        AsyncMock(return_value={"status": "ok", "capacityEnabled": True, "contractVersion": 1}),
    )
    calls = []
    with pytest.raises(HTTPException):
        async with execution_authority.operation(
            SimpleNamespace(run_id="r", workspace_id="w"), {}, 1000
        ):
            calls.append("upstream")
    assert calls == []


@pytest.mark.asyncio
async def test_provider_request_hook_does_not_replay_after_uncertain_dispatch(monkeypatch):
    import httpx

    from app.execution_capacity import execution_authority, provider_dispatch_hook

    monkeypatch.setattr(
        execution_authority,
        "_post",
        AsyncMock(return_value={"status": "ok", "contractVersion": 1, "capacityEnabled": False}),
    )
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"), {}, 1000
    ):
        await provider_dispatch_hook(httpx.Request("POST", "https://provider.test"))
        with pytest.raises(HTTPException, match="replayed"):
            await provider_dispatch_hook(httpx.Request("POST", "https://provider.test"))


@pytest.mark.asyncio
async def test_disabled_suspended_run_calls_no_provider_or_tool(monkeypatch):
    from test_llm_stream import build_llm_stream_payload, build_token_claims

    from app.api.handlers_llm_stream import stream_generation
    from app.api.handlers_tool_call import execute_tool_call
    from app.api.tool_call_contract import ToolCallRequest
    from app.auth.claims import TokenClaims
    from app.auth.jwt_validator import TokenContext
    from app.execution_capacity import execution_authority
    from app.llm.service import NormalizedLLMRequest

    monkeypatch.setattr(
        execution_authority, "_post", AsyncMock(side_effect=HTTPException(409, "suspended"))
    )
    provider_calls = AsyncMock()
    tool_calls = AsyncMock()
    monkeypatch.setattr("app.api.handlers_llm_stream.get_adapter", provider_calls)
    monkeypatch.setattr("app.api.handlers_tool_call.mcp_transport.call_tool", tool_calls)
    claims = TokenClaims(**build_token_claims())
    with pytest.raises(HTTPException):
        await stream_generation(NormalizedLLMRequest(**build_llm_stream_payload()), claims)
    req = ToolCallRequest(
        run_id=claims.run_id,
        workspace_id=claims.workspace_id,
        target_id=claims.target_id,
        target_type=claims.target_type,
        tool="test",
        arguments={},
    )
    with pytest.raises(HTTPException):
        await execute_tool_call(req, TokenContext(claims=claims, token="verified-test-token"))
    provider_calls.assert_not_called()
    tool_calls.assert_not_called()


@pytest.mark.asyncio
async def test_definitive_provider_rejection_retries_with_fresh_operation(monkeypatch):
    import httpx

    from app.config.settings import settings
    from app.execution_capacity import (
        execution_authority,
        provider_dispatch_hook,
        provider_response_hook,
    )

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", True)
    calls = []

    async def post(run_id, action, body):
        calls.append((action, dict(body)))
        return {"status": "ok", "contractVersion": 1, "capacityEnabled": True}

    monkeypatch.setattr(execution_authority, "_post", post)
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"),
        {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "2"},
        1000,
    ):
        request = httpx.Request("POST", "https://provider.test")
        await provider_dispatch_hook(request)
        await provider_response_hook(httpx.Response(400, request=request))
        await provider_dispatch_hook(request)
    begins = [body["operationId"] for action, body in calls if action == "operations/begin"]
    finishes = [body["operationId"] for action, body in calls if action == "operations/finish"]
    assert len(begins) == 2
    assert begins[0] != begins[1]
    assert begins == finishes


@pytest.mark.asyncio
async def test_first_provider_http_request_checks_current_owner(monkeypatch):
    import httpx

    from app.config.settings import settings
    from app.execution_capacity import execution_authority, provider_dispatch_hook

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", True)
    valid_lease = True
    upstream = []

    async def post(run_id, action, body):
        if action == "operations/begin" and not valid_lease:
            raise HTTPException(409, "lease lost")
        return {"status": "ok", "capacityEnabled": True, "contractVersion": 1}

    def provider(request):
        upstream.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(execution_authority, "_post", post)
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"),
        {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "2"},
        1000,
    ):
        valid_lease = False
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider),
            event_hooks={"request": [provider_dispatch_hook]},
        ) as client:
            with pytest.raises(HTTPException, match="lease lost"):
                await client.post("https://provider.test")
    assert upstream == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_enabled", [False, True])
async def test_mcp_actual_tool_call_rechecks_after_initialize(monkeypatch, capacity_enabled):
    import json

    from test_mcp_transport import StrictStreamableMcpServer, transport_for

    from app.config.settings import settings
    from app.execution_capacity import execution_authority
    from app.mcp.egress_policy import ValidatedMcpRequestTarget

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", capacity_enabled)
    initialized = False

    async def post(run_id, action, body):
        if initialized and (
            (not capacity_enabled and action == "authorize")
            or (capacity_enabled and action == "operations/begin")
        ):
            raise HTTPException(409, "authority lost during handshake")
        return {"status": "ok", "capacityEnabled": capacity_enabled, "contractVersion": 1}

    monkeypatch.setattr(execution_authority, "_post", post)
    server = StrictStreamableMcpServer()

    def peer(request):
        nonlocal initialized
        result = server(request)
        if request.method == "POST" and json.loads(request.content).get("method") == "initialize":
            initialized = True
        return result

    target = ValidatedMcpRequestTarget(
        original_url="http://mcp.example/mcp",
        connection_url="http://mcp.example/mcp",
        host_header="mcp.example",
        extensions={},
    )
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.prepare_mcp_egress_request",
        AsyncMock(return_value=target),
    )
    headers = {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "2"}
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"), headers, 1000
    ):
        result = await transport_for(peer).call_tool(
            "http://mcp.example/mcp", "weather.lookup", {}, 1000
        )
    methods = [json.loads(r.content).get("method") for r in server.requests if r.method == "POST"]
    assert initialized
    assert "tools/call" not in methods
    assert result["isError"] is True


@pytest.mark.asyncio
async def test_mcp_registration_occurs_after_initialize_and_finishes_once(monkeypatch):
    import json

    from test_mcp_transport import StrictStreamableMcpServer, transport_for

    from app.config.settings import settings
    from app.execution_capacity import execution_authority
    from app.mcp.egress_policy import ValidatedMcpRequestTarget

    monkeypatch.setattr(settings, "WORKSPACE_CAPACITY_ENABLED", True)
    trace = []

    async def post(run_id, action, body):
        trace.append((action, body))
        return {"status": "ok", "capacityEnabled": True, "contractVersion": 1}

    monkeypatch.setattr(execution_authority, "_post", post)
    server = StrictStreamableMcpServer()

    def peer(request):
        if request.method == "POST":
            trace.append((json.loads(request.content).get("method"), {}))
        return server(request)

    target = ValidatedMcpRequestTarget(
        original_url="http://mcp.example/mcp",
        connection_url="http://mcp.example/mcp",
        host_header="mcp.example",
        extensions={},
    )
    monkeypatch.setattr(
        "app.mcp.transports.http_transport.prepare_mcp_egress_request",
        AsyncMock(return_value=target),
    )
    async with execution_authority.operation(
        SimpleNamespace(run_id="r", workspace_id="w"),
        {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "2"},
        1000,
    ):
        result = await transport_for(peer).call_tool(
            "http://mcp.example/mcp", "weather.lookup", {}, 1000
        )
    assert result["isError"] is False
    actions = [a for a, _ in trace]
    assert (
        actions.index("initialize")
        < actions.index("operations/begin")
        < actions.index("tools/call")
    )
    assert actions.index("tools/call") < actions.index("operations/finish")
    assert actions.count("operations/begin") == actions.count("operations/finish") == 1
