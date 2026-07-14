import base64
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import app.internal_transport as internal_transport_module
from app.auth.service_token import require_admin_service_token
from app.config.settings import Settings


def generated_kek() -> str:
    return base64.b64encode(bytes([9]) * 32).decode()


def production_settings(**overrides):
    values = {
        "APP_ENV": "production",
        "ADMIN_API_TOKEN": "gateway_admin_token_0123456789abcdef",
        "DATABASE_URL": (
            "postgresql+asyncpg://gateway_user:gateway_db_password_0123456789"
            "@gateway-postgres:5432/gateway"
        ),
        "AUTH_JWKS_URL": "http://control-plane:8081/api/v1/auth/jwks.json",
        "SECRETS_KEK_BASE64": generated_kek(),
        "SECRETS_CACHE_TTL_SEC": 0,
        "SECRETS_BACKEND": "database",
        "REDIS_URL": "redis://gateway-redis:6379/0",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_settings_accept_generated_secrets():
    settings = production_settings()

    assert settings.APP_ENV == "production"
    assert settings.ADMIN_API_TOKEN.startswith("gateway_admin_token")
    assert settings.SECRETS_CACHE_TTL_SEC == 0


def test_production_settings_accept_release_qualified_control_plane_jwks_url():
    settings = production_settings(
        AUTH_JWKS_URL=(
            "http://acornops-acornops-platform-control-plane:8081"
            "/api/v1/auth/jwks.json"
        ),
    )

    assert settings.AUTH_JWKS_URL.startswith(
        "http://acornops-acornops-platform-control-plane:8081"
    )


def test_internal_transport_tls_defaults_disabled():
    settings = Settings()

    assert settings.INTERNAL_TRANSPORT_TLS_ENABLED is False
    assert settings.BUILTIN_MCP_SERVER_NAME == "acornops-cluster-agent"
    assert settings.BUILTIN_MCP_SERVER_URL == "http://control-plane:8081/internal/v1/mcp"


def test_builtin_transport_timeout_leaves_headroom_for_producer_error():
    assert internal_transport_module.builtin_tool_transport_timeout_seconds(20_000) == 25.0
    assert internal_transport_module.builtin_tool_transport_timeout_seconds(1) > 5.0


def test_mcp_result_ceiling_cannot_exceed_two_mibibytes():
    with pytest.raises(ValidationError):
        Settings(MCP_MAX_TOOL_RESULT_BYTES=2 * 1024 * 1024 + 1)


def test_builtin_transport_envelope_has_bounded_headroom():
    settings = Settings()

    assert settings.BUILTIN_MCP_MAX_RESPONSE_BYTES == 3 * 1024 * 1024
    with pytest.raises(ValidationError):
        Settings(BUILTIN_MCP_MAX_RESPONSE_BYTES=2 * 1024 * 1024)


def test_internal_transport_tls_requires_files_and_https_urls(tmp_path: Path):
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            INTERNAL_TRANSPORT_TLS_ENABLED=True,
            AUTH_JWKS_URL="http://control-plane:8081/api/v1/auth/jwks.json",
            BUILTIN_MCP_SERVER_URL="http://control-plane:8081/internal/v1/mcp",
        )

    message = str(exc_info.value)
    assert "INTERNAL_TRANSPORT_TLS_CERT_FILE" in message

    ca_file = tmp_path / "ca.crt"
    cert_file = tmp_path / "tls.crt"
    key_file = tmp_path / "tls.key"
    for path in (ca_file, cert_file, key_file):
        path.write_text("test", encoding="utf-8")

    settings = Settings(
        INTERNAL_TRANSPORT_TLS_ENABLED=True,
        INTERNAL_TRANSPORT_TLS_CA_FILE=str(ca_file),
        INTERNAL_TRANSPORT_TLS_CERT_FILE=str(cert_file),
        INTERNAL_TRANSPORT_TLS_KEY_FILE=str(key_file),
        AUTH_JWKS_URL="https://control-plane.acornops.svc:8443/api/v1/auth/jwks.json",
        BUILTIN_MCP_SERVER_URL="https://control-plane.acornops.svc:8443/internal/v1/mcp",
    )

    assert settings.INTERNAL_TRANSPORT_TLS_ENABLED is True


def test_internal_transport_tls_requires_ca_even_when_client_cert_not_required(tmp_path: Path):
    cert_file = tmp_path / "tls.crt"
    key_file = tmp_path / "tls.key"
    for path in (cert_file, key_file):
        path.write_text("test", encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            INTERNAL_TRANSPORT_TLS_ENABLED=True,
            INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT=False,
            INTERNAL_TRANSPORT_TLS_CERT_FILE=str(cert_file),
            INTERNAL_TRANSPORT_TLS_KEY_FILE=str(key_file),
            AUTH_JWKS_URL="https://control-plane.acornops.svc:8443/api/v1/auth/jwks.json",
            BUILTIN_MCP_SERVER_URL="https://control-plane.acornops.svc:8443/internal/v1/mcp",
        )

    assert "INTERNAL_TRANSPORT_TLS_CA_FILE" in str(exc_info.value)


def test_internal_transport_httpx_kwargs_omit_client_cert_when_not_required(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(internal_transport_module.settings, "INTERNAL_TRANSPORT_TLS_ENABLED", True)
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT",
        False,
    )
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_CA_FILE",
        "/tls/ca.crt",
    )
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_CERT_FILE",
        "/tls/client.crt",
    )
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_KEY_FILE",
        "/tls/client.key",
    )

    assert internal_transport_module.httpx_tls_kwargs() == {"verify": "/tls/ca.crt"}


def test_internal_transport_httpx_kwargs_include_client_cert_when_required(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(internal_transport_module.settings, "INTERNAL_TRANSPORT_TLS_ENABLED", True)
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT",
        True,
    )
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_CA_FILE",
        "/tls/ca.crt",
    )
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_CERT_FILE",
        "/tls/client.crt",
    )
    monkeypatch.setattr(
        internal_transport_module.settings,
        "INTERNAL_TRANSPORT_TLS_KEY_FILE",
        "/tls/client.key",
    )

    assert internal_transport_module.httpx_tls_kwargs() == {
        "verify": "/tls/ca.crt",
        "cert": ("/tls/client.crt", "/tls/client.key"),
    }


@pytest.mark.anyio
async def test_builtin_transport_forwards_tool_call_id(monkeypatch: pytest.MonkeyPatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "name": "restart_workload",
            "arguments": {"namespace": "default"},
            "toolCallId": "call-1",
        }
        return httpx.Response(200, json={"content": [], "isError": False}, request=request)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = await internal_transport_module.post_builtin_mcp_tool(
        "http://control-plane/internal/v1/mcp",
        "restart_workload",
        {"namespace": "default"},
        1000,
        {"Authorization": "Bearer token"},
        "call-1",
    )
    assert result == {"content": [], "isError": False}


@pytest.mark.anyio
async def test_builtin_transport_rejects_oversized_results(monkeypatch: pytest.MonkeyPatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"content":"' + b'x' * (3 * 1024 * 1024) + b'"}',
            request=request,
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ValueError, match="3 MiB"):
        await internal_transport_module.post_builtin_mcp_tool(
            "http://control-plane/internal/v1/mcp", "get_resource", {}, 1000, {}
        )


@pytest.mark.anyio
async def test_builtin_transport_preserves_structured_agent_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "TARGET_AGENT_UNAVAILABLE",
                    "message": "Target agent is temporarily unavailable",
                    "outcome": "not_started",
                    "retryable": True,
                }
            },
            request=request,
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = await internal_transport_module.post_builtin_mcp_tool(
        "http://control-plane/internal/v1/mcp", "get_resource", {}, 1000, {}
    )
    assert result.code == "TARGET_AGENT_UNAVAILABLE"
    assert result.dispatch_outcome == "not_started"
    assert result.retryable is True
    assert result["content"][0]["text"] == "Target agent is temporarily unavailable"


def test_production_settings_reject_placeholders_and_unsafe_jwks():
    with pytest.raises(ValidationError) as exc_info:
        production_settings(
            ADMIN_API_TOKEN="dev_orchestrator_token",
            DATABASE_URL="postgresql+asyncpg://gateway_user:gateway_password@gateway-postgres:5432/gateway",
            AUTH_JWKS_URL="http://mock-auth:8003/jwks.json",
            SECRETS_KEK_BASE64="replace-me-with-32-byte-base64",
            SECRETS_CACHE_TTL_SEC=60,
        )

    message = str(exc_info.value)
    assert "ADMIN_API_TOKEN" in message
    assert "DATABASE_URL" in message
    assert "AUTH_JWKS_URL" in message
    assert "SECRETS_KEK_BASE64" in message
    assert "SECRETS_CACHE_TTL_SEC" in message


def test_production_vault_settings_reject_unsafe_secret_backend_config():
    with pytest.raises(ValidationError) as exc_info:
        production_settings(
            SECRETS_BACKEND="vault",
            VAULT_ADDR="http://localhost:8200",
            VAULT_TOKEN="root",
            VAULT_VERIFY_TLS=False,
        )

    message = str(exc_info.value)
    assert "VAULT_ADDR" in message
    assert "VAULT_TOKEN" in message
    assert "VAULT_VERIFY_TLS" in message


def test_production_settings_reject_deterministic_dev_responses():
    with pytest.raises(ValidationError) as exc_info:
        production_settings(LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES=True)

    assert "LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES" in str(exc_info.value)


@pytest.mark.anyio
async def test_admin_service_token_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str]] = []

    def fake_compare_digest(candidate: str, expected: str) -> bool:
        calls.append((candidate, expected))
        return candidate == expected

    monkeypatch.setattr("app.auth.service_token.settings.ADMIN_API_TOKEN", "expected-token")
    monkeypatch.setattr("app.auth.service_token.secrets.compare_digest", fake_compare_digest)

    await require_admin_service_token("Bearer expected-token")

    assert calls == [("expected-token", "expected-token")]
