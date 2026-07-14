import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.main import app
from app.mcp.registry.models import Tool


@pytest.mark.anyio
async def test_full_tool_call_flow_integrated():
    """
    Tests the full flow from a gateway request to an MCP tool call,
    mocking the external MCP server with respx.
    """
    mock_claims = {
        "iss": "llm-gateway",
        "aud": "execution-gateway",
        "iat": 1234567890,
        "exp": 2234567890,
        "sub": "test-user",
        "run_id": "run_999",
        "workspace_id": "ws_999",
        "target_id": "cl_999",
        "target_type": "kubernetes",
        "session_id": "sess_999",
        "permissions": {
            "allowed_providers": ["openai"],
            "allowed_tools": ["*"],
            "max_output_tokens": 4096,
        },
    }

    # 1. Setup tool in registry
    tool = Tool(
        workspace_id="ws_999",
        target_id="cl_999",
        target_type="kubernetes",
        tool_name="integrated_test_tool",
        mcp_server_url="http://mock-mcp-service:8002",
        enabled=True,
        timeout_ms=5000,
    )

    # Mocking DB to avoid relying on a real running Postgres for this test
    with (
        patch(
            "app.mcp.registry.store.tool_registry.get_tool", new_callable=AsyncMock
        ) as mock_get_tool,
        patch(
            "app.api.handlers_tool_call.mcp_server_registry.get_server_by_url",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_get_tool.return_value = tool

        # 2. Mock external MCP server
        with respx.mock:
            mcp_route = respx.post("http://mock-mcp-service:8002/tools/call").mock(
                return_value=Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "Integrated Success"}],
                        "isError": False,
                    },
                )
            )

            # 3. Override Auth
            from app.auth.claims import TokenClaims
            from app.auth.jwt_validator import validator

            async def override_validate():
                return TokenClaims(**mock_claims)

            app.dependency_overrides[validator.validate] = override_validate

            try:
                # 4. Execute request
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    payload = {
                        "run_id": "run_999",
                        "workspace_id": "ws_999",
                        "target_id": "cl_999",
                        "target_type": "kubernetes",
                        "tool": "integrated_test_tool",
                        "arguments": {"input": "test"},
                    }
                    headers = {"Authorization": "Bearer fake-token"}
                    response = await ac.post("/api/v1/mcp/tool-call", json=payload, headers=headers)

                # 5. Verify
                assert response.status_code == 200
                data = response.json()
                expected_result = [{"type": "text", "text": "Integrated Success"}]
                assert data["full_result"] == expected_result
                assert data["model_context"] == expected_result
                assert data["context_meta"]["strategy"] == "mcp_content"
                assert data["artifact_eligible"] is False
                assert data["is_error"] is False
                assert mcp_route.called

            finally:
                app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_integrated_openai(monkeypatch: pytest.MonkeyPatch):
    """
    Tests the LLM streaming flow, mocking the OpenAI API with respx.
    """
    mock_claims = {
        "iss": "llm-gateway",
        "aud": "execution-gateway",
        "iat": 1234567890,
        "exp": 2234567890,
        "sub": "test-user",
        "run_id": "run_888",
        "workspace_id": "ws_888",
        "target_id": "cl_888",
        "target_type": "kubernetes",
        "session_id": "sess_888",
        "permissions": {
            "allowed_providers": ["openai"],
            "allowed_tools": ["*"],
            "max_output_tokens": 4096,
        },
    }

    # Mock secret store
    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
        mock_get_secret.return_value = "sk-fake-openai-key"

        async def stream_response():
            yield SimpleNamespace(type="response.output_text.delta", delta="Hello")
            yield SimpleNamespace(type="response.output_text.delta", delta=" world")
            yield SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=10, output_tokens=2),
                ),
            )

        class FakeResponses:
            async def create(self, **_kwargs):
                return stream_response()

        class FakeClient:
            def __init__(self, api_key: str):
                del api_key
                self.responses = FakeResponses()

        monkeypatch.setattr("app.llm.adapters.openai_adapter.AsyncOpenAI", FakeClient)

        # Override Auth
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            # Execute request
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                payload = {
                    "run_id": "run_888",
                    "workspace_id": "ws_888",
                    "target_id": "cl_888",
                    "target_type": "kubernetes",
                    "session_id": "sess_888",
                    "provider": "openai",
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "hi"}],
                    "temperature": 0.7,
                    "max_output_tokens": 1000,
                }
                headers = {"Authorization": "Bearer fake-token"}
                response = await ac.post(
                    "/api/v1/llm/generations:stream", json=payload, headers=headers
                )

            assert response.status_code == 200
            lines = response.text.strip().split("\n")
            chunks = [json.loads(line) for line in lines]

            assert any(c["type"] == "delta" and c["text"] == "Hello" for c in chunks)
            assert any(
                c["type"] == "final" and c["usage"]["input_tokens"] == 10 for c in chunks
            )

        finally:
            app.dependency_overrides.clear()
