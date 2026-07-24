from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.handlers_tool_call import (
    BUILTIN_MCP_BRIDGE_NOT_CONFIGURED,
    _mark_unknown_write_contract,
    _tool_execution_error_response,
    _tool_transport_error_response,
)
from app.api.tool_result_normalization import ToolCallResponse
from app.examples import (
    EXAMPLE_RUN_ID,
    EXAMPLE_SESSION_ID,
    EXAMPLE_TARGET_ID,
    EXAMPLE_WORKSPACE_ID,
)
from app.main import app
from app.mcp.registry.models import McpServer, Tool
from app.mcp.tool_identity import model_tool_alias
from app.mcp.transports.http_transport import McpToolTransportError

EXAMPLE_SERVER_ID = "11111111-1111-1111-1111-111111111111"
EXAMPLE_TOOL_ALIAS = model_tool_alias(EXAMPLE_SERVER_ID, "get_weather")

BASE_CLAIMS = {
    "iss": "llm-gateway",
    "aud": "execution-gateway",
    "iat": 1234567890,
    "exp": 2234567890,
    "sub": "test-user",
    "user_id": "test-user",
    "run_id": EXAMPLE_RUN_ID,
    "workspace_id": EXAMPLE_WORKSPACE_ID,
    "target_id": EXAMPLE_TARGET_ID,
    "target_type": "kubernetes",
    "session_id": EXAMPLE_SESSION_ID,
    "permissions": {
        "allowed_providers": ["openai"],
        "allowed_tools": ["*"],
        "allowed_tool_refs": [
            {"server_id": "11111111-1111-1111-1111-111111111111", "tool_name": "get_weather"}
        ],
        "max_output_tokens": 4096,
    },
}


def test_post_dispatch_write_timeout_is_not_retryable():
    write_result = _tool_execution_error_response(httpx.ReadTimeout("timed out"), "write")
    read_result = _tool_execution_error_response(httpx.ReadTimeout("timed out"), "read")
    assert write_result.full_result == {
        "code": "TOOL_TIMEOUT", "message": "Tool execution timed out",
        "retryable": False, "outcome": "unknown",
    }
    assert read_result.full_result == {
        "code": "TOOL_TIMEOUT", "message": "Tool execution timed out", "retryable": True,
    }


def test_invalid_post_dispatch_jsonrpc_write_result_is_unknown_and_not_retryable():
    transport_error = McpToolTransportError(
        {"isError": True, "content": [{"type": "text", "text": "Upstream tool error"}]},
        code="MCP_RESULT_INVALID",
        dispatch_outcome="unknown",
        retryable=False,
    )

    response = _tool_transport_error_response(transport_error, "write")

    assert response.full_result == {
        "code": "MCP_RESULT_INVALID",
        "message": "Upstream tool error",
        "retryable": False,
        "outcome": "unknown",
    }


@pytest.mark.parametrize(
    "code", ["TOOL_RESULT_SCHEMA_INVALID", "TOOL_RESULT_CONTRACT_INVALID"]
)
def test_invalid_write_result_contract_has_unknown_outcome(code: str):
    error = {"code": code, "message": "Invalid result"}
    response = _mark_unknown_write_contract(
        ToolCallResponse(
            full_result=error,
            model_context=error,
            context_meta={"strategy": "schema_error", "original_bytes": 1, "context_bytes": 1},
            artifact_eligible=False,
            is_error=True,
        ),
        "write",
    )
    assert response.full_result["outcome"] == "unknown"
    assert response.full_result["retryable"] is False


def build_token_claims(**overrides):
    claims = deepcopy(BASE_CLAIMS)
    permissions = overrides.pop("permissions", None)
    claims.update(overrides)
    if permissions:
        claims["permissions"] = {**claims["permissions"], **permissions}
    return claims


def build_tool_call_payload(**overrides):
    payload = {
        "run_id": EXAMPLE_RUN_ID,
        "workspace_id": EXAMPLE_WORKSPACE_ID,
        "target_id": EXAMPLE_TARGET_ID,
        "target_type": "kubernetes",
        "tool": EXAMPLE_TOOL_ALIAS,
        "tool_ref": {"server_id": EXAMPLE_SERVER_ID, "tool_name": "get_weather"},
        "arguments": {"location": "SF"},
    }
    payload.update(overrides)
    return payload


def build_workflow_tool_call_payload(**overrides):
    payload = {
        "run_id": EXAMPLE_RUN_ID,
        "workspace_id": EXAMPLE_WORKSPACE_ID,
        "scope": {"type": "workspace"},
        "workflow_id": "workspace-tool-exposure-audit",
        "execution_id": "workflow-execution-1",
        "workflow_session_id": "workflow-session-1",
        "executor_role": "specialist",
        "agent_id": "agent-1",
        "agent_version": 1,
        "tool": "mcp.tools.list",
        "tool_ref": {"server_id": EXAMPLE_SERVER_ID, "tool_name": "mcp.tools.list"},
        "arguments": {},
    }
    payload.update(overrides)
    return payload


def reviewed_tool(**overrides):
    values = {
        "server_id": EXAMPLE_SERVER_ID,
        "workspace_id": EXAMPLE_WORKSPACE_ID,
        "target_id": EXAMPLE_TARGET_ID,
        "target_type": "kubernetes",
        "tool_name": "get_weather",
        "mcp_server_url": "http://mock-mcp:8002",
        "enabled": True,
        "timeout_ms": 10000,
        "capability": "read",
        "review_state": "approved",
    }
    values.update(overrides)
    return Tool(**values)


def enabled_server(**overrides):
    values = {
        "id": EXAMPLE_SERVER_ID,
        "workspace_id": EXAMPLE_WORKSPACE_ID,
        "target_id": EXAMPLE_TARGET_ID,
        "target_type": "kubernetes",
        "server_name": "weather",
        "server_url": "http://mock-mcp:8002",
        "enabled": True,
        "auth_type": "none",
        "credential_mode": "none",
    }
    values.update(overrides)
    return McpServer(**values)


@pytest.mark.anyio
async def test_tool_call_contract():
    mock_claims = build_token_claims()

    # Mock tool registry
    mock_tool = reviewed_tool()

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=enabled_server(),
        ),
    ):
        mock_get_tool.return_value = mock_tool

        # Mock MCP transport
        with patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool", new_callable=AsyncMock
        ) as mock_call_tool:
            mock_call_tool.return_value = {
                "content": [{"type": "text", "text": "Sunny"}],
                "isError": False,
            }

            # Mock JWT validation
            from app.auth.claims import TokenClaims
            from app.auth.jwt_validator import validator

            async def override_validate():
                return TokenClaims(**mock_claims)

            app.dependency_overrides[validator.validate] = override_validate

            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    headers = {"Authorization": "Bearer fake-token"}
                    response = await ac.post(
                        "/api/v1/mcp/tool-call",
                        json=build_tool_call_payload(),
                        headers=headers,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["full_result"] == [{"type": "text", "text": "Sunny"}]
                assert data["model_context"] == [{"type": "text", "text": "Sunny"}]
                assert data["context_meta"]["strategy"] == "mcp_content"
                assert data["artifact_eligible"] is False
                assert data["is_error"] is False
                mock_get_tool.assert_awaited_once_with(
                    EXAMPLE_WORKSPACE_ID,
                    EXAMPLE_TARGET_ID,
                    "get_weather",
                    target_type="kubernetes",
                    server_id=EXAMPLE_SERVER_ID,
                )
            finally:
                app.dependency_overrides.clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("claims_permissions", "payload_overrides", "expected_detail"),
    [
        (
            {
                "allowed_tool_refs": [
                    {"server_id": EXAMPLE_SERVER_ID, "tool_name": "approved_tool"}
                ]
            },
            {},
            "not permitted",
        ),
        ({"allowed_tool_refs": []}, {}, "not permitted"),
        ({}, {"target_id": "other-cluster"}, "scope mismatch"),
    ],
)
async def test_tool_call_rejects_permission_and_scope_mismatches(
    claims_permissions: dict[str, object],
    payload_overrides: dict[str, object],
    expected_detail: str,
):
    mock_claims = build_token_claims(permissions=claims_permissions)

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool", new_callable=AsyncMock
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = reviewed_tool()
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(**payload_overrides),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 403
            assert expected_detail in response.json()["detail"].lower()
            mock_call_tool.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_tool_call_rejects_internal_model_only_tool_even_with_wildcard_permission():
    mock_claims = build_token_claims(permissions={"allowed_tools": ["*"]})

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool", new_callable=AsyncMock
        ) as mock_call_tool,
    ):
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(tool="_acornops_load_skill"),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 403
            assert "reserved for internal model-only" in response.json()["detail"].lower()
            mock_get_tool.assert_not_awaited()
            mock_call_tool.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_tool_call_sanitizes_server_auth_backend_failures():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="http://mock-mcp:8002",
        enabled=True,
        timeout_ms=10000,
        capability="read",
        review_state="approved",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        server_name="weather",
        server_url="http://mock-mcp:8002",
        enabled=True,
        auth_type="bearer_token",
        credential_mode="individual",
        auth_header_name="Authorization",
        auth_header_prefix="Bearer ",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ),
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(
                status="connected",
                verified_tool_names=["get_weather"],
                access_secret_name="mcp_credential::weather::test-user",
            ),
        ),
        patch(
            "app.api.mcp_runtime_auth.secret_store.get_secret",
            new_callable=AsyncMock,
            side_effect=RuntimeError("vault weather-token unavailable"),
        ) as mock_get_secret,
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool", new_callable=AsyncMock
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "MCP_SECRET_BACKEND_UNAVAILABLE"
            assert "weather" not in response.json()["detail"]
            assert "weather-token" not in response.json()["detail"]
            assert "vault" not in response.json()["detail"]
            mock_get_secret.assert_awaited()
            mock_call_tool.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_tool_call_sanitizes_execution_failures():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="http://mock-mcp:8002",
        enabled=True,
        timeout_ms=10000,
        capability="read",
        review_state="approved",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=enabled_server(),
        ),
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("http://internal-mcp leaked backend error"),
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["is_error"] is True
            assert data["full_result"]["code"] == "TOOL_EXECUTION_FAILED"
            assert data["full_result"]["message"] == "Tool execution failed"
            assert "outcome" not in data["full_result"]
            assert data["full_result"]["retryable"] is False
            assert data["model_context"] == data["full_result"]
            assert "internal-mcp" not in data["full_result"]["message"]
            mock_call_tool.assert_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_tool_call_validates_input_schema():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="http://mock-mcp:8002",
        enabled=True,
        timeout_ms=10000,
        capability="read",
        review_state="approved",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
            "additionalProperties": False,
        },
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool", new_callable=AsyncMock
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(arguments={}),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 400
            detail = response.json()["detail"]
            assert detail["code"] == "TOOL_ARGS_INVALID"
            assert "invalid arguments" in detail["message"].lower()
            mock_call_tool.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_tool_call_merges_public_and_secret_headers():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="http://mock-mcp:8002",
        enabled=True,
        timeout_ms=10000,
        capability="read",
        review_state="approved",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        server_name="weather",
        server_url="http://mock-mcp:8002",
        enabled=True,
        auth_type="bearer_token",
        credential_mode="individual",
        auth_header_name="Authorization",
        auth_header_prefix="Bearer ",
        public_headers={"x-public-header": "true", "x-run-id": "spoofed"},
        provenance_type="manual",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ),
            patch(
                "app.api.handlers_tool_call.connection_request_headers",
                new_callable=AsyncMock,
                return_value={
                    "x-workspace-id": EXAMPLE_WORKSPACE_ID,
                    "x-target-id": EXAMPLE_TARGET_ID,
                    "x-target-type": "kubernetes",
                    "x-run-id": EXAMPLE_RUN_ID,
                    "x-public-header": "true",
                    "Authorization": "Bearer secret-token",
                },
            ) as mock_connection_headers,
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
            return_value={"content": [{"type": "text", "text": "Sunny"}], "isError": False},
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 200
            mock_connection_headers.assert_awaited_once_with(
                mock_server,
                ANY,
                "get_weather",
                platform_headers={
                    "x-workspace-id": EXAMPLE_WORKSPACE_ID,
                    "x-target-id": EXAMPLE_TARGET_ID,
                    "x-target-type": "kubernetes",
                    "x-run-id": EXAMPLE_RUN_ID,
                },
            )
            assert mock_call_tool.await_args.args[4] == {
                "x-workspace-id": EXAMPLE_WORKSPACE_ID,
                "x-target-id": EXAMPLE_TARGET_ID,
                "x-target-type": "kubernetes",
                "x-run-id": EXAMPLE_RUN_ID,
                "x-public-header": "true",
                "Authorization": "Bearer secret-token",
            }
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_builtin_tool_call_forwards_run_token_without_configured_mcp_headers():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="http://control-plane:8081/internal/v1/mcp",
        enabled=True,
        timeout_ms=10000,
        capability="read",
        review_state="approved",
        source="builtin",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        server_name="acornops-target-agent",
        server_url="http://control-plane:8081/internal/v1/mcp",
        enabled=True,
        auth_type="bearer_token",
        auth_header_name="Authorization",
        auth_header_prefix="Bearer ",
        public_headers={"x-public-header": "true", "x-run-id": "spoofed"},
        provenance_type="builtin",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ),
        patch(
            "app.api.handlers_tool_call.post_builtin_mcp_tool",
            new_callable=AsyncMock,
            return_value={"content": [{"type": "text", "text": "Sunny"}], "isError": False},
        ) as mock_builtin_call,
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(tool_call_id="call-1"),
                    headers={"Authorization": "Bearer run-scoped-jwt"},
                )

            assert response.status_code == 200
            mock_call_tool.assert_not_awaited()
            assert mock_builtin_call.await_args.args[4] == {
                "Authorization": "Bearer run-scoped-jwt",
            }
            assert mock_builtin_call.await_args.args[5] == "call-1"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_workspace_workflow_builtin_tool_requires_registry_entry_and_forwards_run_token():
    mock_claims = build_token_claims(
        scope={"type": "workspace"},
        target_id=None,
        target_type=None,
        workflow_id="workspace-tool-exposure-audit",
        execution_id="workflow-execution-1",
        workflow_session_id="workflow-session-1",
        executor_role="specialist",
        agent_id="agent-1",
        agent_version=1,
        permissions={
            "allowed_tools": ["mcp.tools.list"],
            "allowed_tool_refs": [{"server_id": EXAMPLE_SERVER_ID, "tool_name": "mcp.tools.list"}],
            "allowed_tool_operations": {"mcp.tools.list": "read"},
            "context_grants": ["workspace_metadata"],
        },
    )
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        scope_type="agent",
        agent_id="agent-1",
        target_id="agent-1",
        target_type="agent",
        tool_name="mcp.tools.list",
        mcp_server_url="http://control-plane:8081/internal/v1/mcp",
        enabled=True,
        timeout_ms=10000,
        source="builtin",
        capability="read",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        scope_type="agent",
        agent_id="agent-1",
        target_id="agent-1",
        target_type="agent",
        server_name="acornops-target-agent",
        server_url="http://control-plane:8081/internal/v1/mcp",
        enabled=True,
        auth_type="none",
        provenance_type="builtin",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ) as mock_get_server,
        patch(
            "app.api.handlers_tool_call.post_builtin_mcp_tool",
            new_callable=AsyncMock,
            return_value={
                "content": [{"type": "text", "text": '{"tools":["mcp.tools.list"]}'}],
                "isError": False,
            },
        ) as mock_builtin_call,
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_workflow_tool_call_payload(),
                    headers={"Authorization": "Bearer workflow-run-jwt"},
                )

            assert response.status_code == 200
            payload = response.json()
            assert payload["full_result"] == [
                {"type": "text", "text": '{"tools":["mcp.tools.list"]}'}
            ]
            assert payload["model_context"] == {"tools": ["mcp.tools.list"]}
            assert payload["artifact_eligible"] is False
            assert payload["is_error"] is False
            mock_get_tool.assert_awaited_once_with(
                EXAMPLE_WORKSPACE_ID,
                "agent-1",
                "mcp.tools.list",
                target_type="agent",
                server_id=EXAMPLE_SERVER_ID,
            )
            mock_get_server.assert_awaited_once()
            mock_call_tool.assert_not_awaited()
            assert mock_builtin_call.await_args.args[0] == "http://control-plane:8081/internal/v1/mcp"
            assert mock_builtin_call.await_args.args[1] == "mcp.tools.list"
            assert mock_builtin_call.await_args.args[4] == {
                "Authorization": "Bearer workflow-run-jwt",
            }
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_workspace_workflow_tool_call_executes_enabled_remote_registry_tool():
    mock_claims = build_token_claims(
        scope={"type": "workspace"},
        target_id=None,
        target_type=None,
        workflow_id="workspace-tool-exposure-audit",
        execution_id="workflow-execution-1",
        workflow_session_id="workflow-session-1",
        executor_role="specialist",
        agent_id="agent-1",
        agent_version=1,
        permissions={
            "allowed_tools": ["records.list"],
            "allowed_tool_refs": [{"server_id": EXAMPLE_SERVER_ID, "tool_name": "records.list"}],
        },
    )
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        scope_type="agent",
        agent_id="agent-1",
        target_id="agent-1",
        target_type="agent",
        tool_name="records.list",
        mcp_server_url="https://mcp.example.com/v1",
        enabled=True,
        timeout_ms=10000,
        source="mcp",
        capability="read",
        review_state="approved",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        scope_type="agent",
        agent_id="agent-1",
        target_id="agent-1",
        target_type="agent",
        server_name="operations-catalog",
        server_url="https://mcp.example.com/v1",
            enabled=True,
            auth_type="none",
            credential_mode="none",
            public_headers={"x-client-version": "test"},
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool",
            new_callable=AsyncMock,
            return_value=mock_tool,
        ),
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ),
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
            return_value={"content": [{"type": "text", "text": "ok"}], "isError": False},
        ) as mock_call_tool,
        patch(
            "app.api.handlers_tool_call.post_builtin_mcp_tool",
            new_callable=AsyncMock,
        ) as mock_builtin_call,
    ):
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_workflow_tool_call_payload(
                        tool=model_tool_alias(EXAMPLE_SERVER_ID, "records.list"),
                        tool_ref={"server_id": EXAMPLE_SERVER_ID, "tool_name": "records.list"},
                    ),
                    headers={"Authorization": "Bearer workflow-run-jwt"},
                )
            assert response.status_code == 200
            assert response.json()["is_error"] is False
            mock_builtin_call.assert_not_awaited()
            headers = mock_call_tool.await_args.args[4]
            assert headers["x-workspace-id"] == EXAMPLE_WORKSPACE_ID
            assert headers["x-workflow-execution-id"] == "workflow-execution-1"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_workspace_workflow_tool_call_rejects_internal_model_only_tool_before_bridge():
    mock_claims = build_token_claims(
        scope={"type": "workspace"},
        target_id=None,
        target_type=None,
        workflow_id="workspace-tool-exposure-audit",
        execution_id="workflow-execution-1",
        workflow_session_id="workflow-session-1",
        executor_role="specialist",
        agent_id="agent-1",
        agent_version=1,
        permissions={"allowed_tools": ["*"]},
    )

    with patch(
        "app.api.handlers_tool_call.post_builtin_mcp_tool",
        new_callable=AsyncMock,
    ) as mock_builtin_call:
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_workflow_tool_call_payload(tool="_acornops_load_skill"),
                    headers={"Authorization": "Bearer workflow-run-jwt"},
                )

            assert response.status_code == 403
            assert "reserved for internal model-only" in response.json()["detail"].lower()
            mock_builtin_call.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_builtin_source_does_not_forward_run_token_to_non_builtin_server():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="https://mcp.example.com",
        enabled=True,
        timeout_ms=10000,
        source="builtin",
        capability="read",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        server_name="remote",
        server_url="https://mcp.example.com",
        enabled=True,
        auth_type="none",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ),
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(),
                    headers={"Authorization": "Bearer run-scoped-jwt"},
                )

            assert response.status_code == 500
            assert response.json()["detail"] == BUILTIN_MCP_BRIDGE_NOT_CONFIGURED
            mock_call_tool.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_tool_call_rejects_invalid_secret_header_value():
    mock_claims = build_token_claims()
    mock_tool = Tool(
        server_id=EXAMPLE_SERVER_ID,
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        tool_name="get_weather",
        mcp_server_url="http://mock-mcp:8002",
        enabled=True,
        timeout_ms=10000,
        capability="read",
        review_state="approved",
    )
    mock_server = McpServer(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type="kubernetes",
        server_name="weather",
        server_url="http://mock-mcp:8002",
        enabled=True,
        auth_type="bearer_token",
        credential_mode="individual",
        auth_header_prefix="Bearer ",
    )

    with (
        patch(
            "app.api.handlers_tool_call.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server",
            new_callable=AsyncMock,
            return_value=mock_server,
        ),
        patch(
            "app.api.handlers_tool_call.connection_request_headers",
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=409,
                detail={"code": "MCP_CONNECTION_REQUIRED"},
            ),
        ),
        patch(
            "app.api.handlers_tool_call.mcp_transport.call_tool",
            new_callable=AsyncMock,
        ) as mock_call_tool,
    ):
        mock_get_tool.return_value = mock_tool

        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                response = await ac.post(
                    "/api/v1/mcp/tool-call",
                    json=build_tool_call_payload(),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "MCP_CONNECTION_REQUIRED"
            mock_call_tool.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()
