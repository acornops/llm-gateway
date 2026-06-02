import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.examples import (
    EXAMPLE_RUN_ID,
    EXAMPLE_SESSION_ID,
    EXAMPLE_TARGET_ID,
    EXAMPLE_WORKSPACE_ID,
)
from app.llm.service import StreamEvent
from app.main import app

BASE_CLAIMS = {
    "iss": "llm-gateway",
    "aud": "execution-gateway",
    "iat": 1234567890,
    "exp": 2234567890,
    "sub": "test-user",
    "run_id": EXAMPLE_RUN_ID,
    "workspace_id": EXAMPLE_WORKSPACE_ID,
    "target_id": EXAMPLE_TARGET_ID,
    "target_type": "kubernetes",
    "session_id": EXAMPLE_SESSION_ID,
    "permissions": {
        "allowed_providers": ["openai"],
        "allowed_tools": ["*"],
        "max_output_tokens": 4096,
    },
}


def build_token_claims(**overrides):
    claims = deepcopy(BASE_CLAIMS)
    permissions = overrides.pop("permissions", None)
    claims.update(overrides)
    if permissions:
        claims["permissions"] = {**claims["permissions"], **permissions}
    return claims


def build_llm_stream_payload(**overrides):
    payload = {
        "run_id": EXAMPLE_RUN_ID,
        "workspace_id": EXAMPLE_WORKSPACE_ID,
        "target_id": EXAMPLE_TARGET_ID,
        "target_type": "kubernetes",
        "session_id": EXAMPLE_SESSION_ID,
        "provider": "openai",
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "max_output_tokens": 1000,
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_llm_stream_contract():
    mock_claims = build_token_claims()

    # Mock secret store
    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
        mock_get_secret.return_value = "fake-api-key"

        # Mock JWT validation using dependency overrides
        from app.auth.claims import TokenClaims
        from app.auth.jwt_validator import validator

        async def override_validate():
            return TokenClaims(**mock_claims)

        app.dependency_overrides[validator.validate] = override_validate

        try:
            # Mock OpenAI adapter
            with patch("app.llm.adapters.openai_adapter.OpenAIAdapter.stream") as mock_stream:

                async def mock_generator(*args, **kwargs):
                    yield StreamEvent(type="delta", text="Hello")
                    yield StreamEvent(type="delta", text=" world")
                    yield StreamEvent(
                        type="final",
                        usage={"input_tokens": 10, "output_tokens": 2, "tool_calls": 0},
                    )

                mock_stream.side_effect = mock_generator

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    headers = {"Authorization": "Bearer fake-token"}
                    response = await ac.post(
                        "/api/v1/llm/chat-completions:stream",
                        json=build_llm_stream_payload(),
                        headers=headers,
                    )

                assert response.status_code == 200
                lines = response.text.strip().split("\n")
                assert len(lines) == 3

                chunks = [json.loads(line) for line in lines]
                assert chunks[0]["type"] == "delta"
                assert chunks[0]["text"] == "Hello"
                assert chunks[1]["type"] == "delta"
                assert chunks[1]["text"] == " world"
                assert chunks[2]["type"] == "final"
                assert chunks[2]["usage"]["input_tokens"] == 10
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "cluster-other"),
        ("session_id", "session-other"),
    ],
)
async def test_llm_stream_rejects_cluster_and_session_scope_mismatch(field: str, value: str):
    mock_claims = build_token_claims()

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(**{field: value}),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 403
            assert "scope mismatch" in response.json()["detail"].lower()
            mock_get_secret.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("claims_permissions", "payload_overrides", "expected_detail"),
    [
        ({"allowed_providers": ["openai"]}, {"provider": "anthropic"}, "not allowed"),
        ({"allowed_models": ["gpt-4o-mini"]}, {}, "not allowed"),
        ({"max_output_tokens": 128}, {}, "exceeds token permission limit"),
        (
            {"allowed_tools": ["approved_tool"]},
            {"tools": [{"name": "blocked_tool"}]},
            "tool(s) not allowed",
        ),
        (
            {"allowed_tools": []},
            {"tools": [{"name": "blocked_tool"}]},
            "tool(s) not allowed",
        ),
    ],
)
async def test_llm_stream_enforces_permission_checks(
    claims_permissions: dict[str, object],
    payload_overrides: dict[str, object],
    expected_detail: str,
):
    mock_claims = build_token_claims(permissions=claims_permissions)
    payload = build_llm_stream_payload(**payload_overrides)

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=payload,
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 403
            assert expected_detail in response.json()["detail"].lower()
            mock_get_secret.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_can_emit_deterministic_dev_tool_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.api.handlers_llm_stream.settings.LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES",
        True,
    )
    mock_claims = build_token_claims(
        target_type="virtual_machine",
        permissions={"allowed_tools": ["get_host_summary"]},
    )

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(
                        target_type="virtual_machine",
                        tools=[{"name": "get_host_summary"}],
                    ),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 200
            chunks = [json.loads(line) for line in response.text.strip().split("\n")]
            assert chunks[0]["type"] == "tool_call"
            assert chunks[0]["tool"] == "get_host_summary"
            assert chunks[1]["type"] == "final"
            mock_get_secret.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_deterministic_dev_skips_tools_that_require_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.api.handlers_llm_stream.settings.LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES",
        True,
    )
    mock_claims = build_token_claims(permissions={"allowed_tools": ["get_resource"]})

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(
                        tools=[
                            {
                                "name": "get_resource",
                                "input_schema": {
                                    "type": "object",
                                    "required": ["kind", "name"],
                                    "properties": {
                                        "kind": {"type": "string"},
                                        "name": {"type": "string"},
                                    },
                                },
                            }
                        ],
                    ),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 200
            chunks = [json.loads(line) for line in response.text.strip().split("\n")]
            assert chunks[0]["type"] == "delta"
            assert "requested diagnostic context" in chunks[0]["text"]
            assert chunks[1]["type"] == "final"
            mock_get_secret.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_deterministic_dev_response_summarizes_tool_feedback(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.api.handlers_llm_stream.settings.LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES",
        True,
    )
    mock_claims = build_token_claims(
        target_type="virtual_machine",
        permissions={"allowed_tools": ["get_host_summary"]},
    )

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(
                        target_type="virtual_machine",
                        messages=[{"role": "user", "content": "Live tool results:\n{}"}],
                        tools=[{"name": "get_host_summary"}],
                    ),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 200
            chunks = [json.loads(line) for line in response.text.strip().split("\n")]
            assert chunks[0]["type"] == "delta"
            assert "Local deterministic response" in chunks[0]["text"]
            assert chunks[1]["type"] == "final"
            mock_get_secret.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_rejects_unknown_provider_names():
    mock_claims = build_token_claims(permissions={"allowed_providers": ["gemini"]})

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret", new_callable=AsyncMock
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(
                        provider="google", model="gemini-2.0-flash"
                    ),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 422
            mock_get_secret.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_returns_503_when_secret_backend_is_unavailable():
    mock_claims = build_token_claims()

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret",
        new_callable=AsyncMock,
        side_effect=RuntimeError("vault unavailable"),
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 503
            assert response.json()["detail"] == "Provider credential backend unavailable"
            assert "vault unavailable" not in response.json()["detail"]
            mock_get_secret.assert_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_llm_stream_missing_provider_credentials_are_sanitized():
    mock_claims = build_token_claims()

    with patch(
        "app.api.handlers_llm_stream.secret_store.get_secret",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_get_secret:
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
                    "/api/v1/llm/chat-completions:stream",
                    json=build_llm_stream_payload(),
                    headers={"Authorization": "Bearer fake-token"},
                )

            assert response.status_code == 500
            assert response.json()["detail"] == "Provider credentials are not configured"
            assert "openai_api_key" not in response.json()["detail"]
            mock_get_secret.assert_awaited()
        finally:
            app.dependency_overrides.clear()
