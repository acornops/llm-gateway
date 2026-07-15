from unittest.mock import AsyncMock, MagicMock

import pytest

import app.outbound_tls as outbound_tls


def test_additional_ca_extends_system_trust(monkeypatch: pytest.MonkeyPatch):
    context = MagicMock()
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )
    monkeypatch.setattr(outbound_tls.ssl, "create_default_context", lambda: context)

    assert outbound_tls.additional_ca_ssl_context() is context
    context.load_verify_locations.assert_called_once_with(cafile="/trust/additional-ca.pem")


def test_httpx_ca_extends_httpx_default_trust(monkeypatch: pytest.MonkeyPatch):
    context = MagicMock()
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )
    monkeypatch.setattr(outbound_tls.httpx, "create_ssl_context", lambda: context)

    assert outbound_tls.httpx_verify() is context
    context.load_verify_locations.assert_called_once_with(cafile="/trust/additional-ca.pem")


def test_internal_ca_context_uses_only_explicit_trust(monkeypatch: pytest.MonkeyPatch):
    context = MagicMock()
    context_factory = MagicMock(return_value=context)
    monkeypatch.setattr(outbound_tls.ssl, "SSLContext", context_factory)

    assert outbound_tls.internal_httpx_ssl_context(
        "/tls/internal-ca.pem",
        "/trust/additional-ca.pem",
    ) is context
    context_factory.assert_called_once_with(outbound_tls.ssl.PROTOCOL_TLS_CLIENT)
    assert context.load_verify_locations.call_count == 2
    context.load_verify_locations.assert_any_call(cafile="/tls/internal-ca.pem")
    context.load_verify_locations.assert_any_call(cafile="/trust/additional-ca.pem")


@pytest.mark.parametrize(
    ("provider", "factory_path", "expected_kwargs"),
    [
        ("openai", "OpenAIHttpClient", {}),
        ("anthropic", "AnthropicHttpClient", {}),
        ("gemini", "httpx.AsyncClient", {"follow_redirects": True}),
    ],
)
def test_provider_client_preserves_sdk_defaults_and_is_reused(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    factory_path: str,
    expected_kwargs: dict,
):
    context = MagicMock()
    client = MagicMock()
    client.is_closed = False
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )
    monkeypatch.setattr(outbound_tls, "_provider_http_clients", {})
    monkeypatch.setattr(outbound_tls, "httpx_additional_ca_ssl_context", lambda: context)
    if factory_path == "httpx.AsyncClient":
        monkeypatch.setattr(outbound_tls.httpx, "AsyncClient", client_factory)
    else:
        monkeypatch.setattr(outbound_tls, factory_path, client_factory)

    assert outbound_tls.provider_http_client(provider) is client
    assert outbound_tls.provider_http_client(provider) is client
    client_factory.assert_called_once_with(verify=context, **expected_kwargs)


@pytest.mark.anyio
async def test_provider_clients_close_at_shutdown(monkeypatch: pytest.MonkeyPatch):
    client = MagicMock()
    client.is_closed = False
    client.aclose = AsyncMock()
    monkeypatch.setattr(outbound_tls, "_provider_http_clients", {"openai": client})

    await outbound_tls.close_provider_http_clients()

    client.aclose.assert_awaited_once_with()
    assert outbound_tls._provider_http_clients == {}


def test_redis_trust_requires_rediss(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )

    assert outbound_tls.redis_tls_kwargs("redis://redis:6379/0") == {}
    assert outbound_tls.redis_tls_kwargs("rediss://redis.example:6379/0") == {
        "ssl_ca_certs": "/trust/additional-ca.pem",
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
    }


def test_database_trust_requires_explicit_tls(monkeypatch: pytest.MonkeyPatch):
    context = MagicMock()
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )
    monkeypatch.setattr(outbound_tls, "additional_ca_ssl_context", lambda: context)

    database_url = "postgresql+asyncpg://db/gateway"
    assert outbound_tls.sqlalchemy_connection_config(database_url) == (database_url, {})
    assert outbound_tls.sqlalchemy_connection_config(
        f"{database_url}?ssl=verify-full"
    ) == (database_url, {"ssl": context})


def test_database_sslmode_is_normalized_for_asyncpg(monkeypatch: pytest.MonkeyPatch):
    context = MagicMock()
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )
    monkeypatch.setattr(outbound_tls, "additional_ca_ssl_context", lambda: context)

    database_url, connect_args = outbound_tls.sqlalchemy_connection_config(
        "postgresql+asyncpg://db/gateway?sslmode=verify-full&application_name=gateway"
    )

    assert database_url == "postgresql+asyncpg://db/gateway?application_name=gateway"
    assert connect_args == {"ssl": context}


def test_database_disabled_sslmode_does_not_enable_tls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )

    assert outbound_tls.sqlalchemy_connection_config(
        "postgresql+asyncpg://db/gateway?sslmode=disable"
    ) == ("postgresql+asyncpg://db/gateway", {})


def test_tls_verification_can_only_be_disabled_by_the_calling_feature(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        outbound_tls.settings, "ADDITIONAL_CA_BUNDLE_FILE", "/trust/additional-ca.pem"
    )
    assert outbound_tls.httpx_verify(verify_tls=False) is False
