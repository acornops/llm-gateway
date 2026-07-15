"""Additive trust configuration shared by outbound dependency clients."""

from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from anthropic import DefaultAsyncHttpxClient as AnthropicHttpClient
from openai import DefaultAsyncHttpxClient as OpenAIHttpClient

from app.config.settings import settings

_provider_http_clients: dict[str, httpx.AsyncClient] = {}
_TLS_ENABLED_VALUES = {"1", "true", "require", "verify-ca", "verify-full"}
_TLS_DISABLED_VALUES = {"0", "false", "disable"}


def additional_ca_ssl_context() -> ssl.SSLContext:
    """Return system trust extended with the operator-provided CA bundle."""
    context = ssl.create_default_context()
    if settings.ADDITIONAL_CA_BUNDLE_FILE:
        context.load_verify_locations(cafile=settings.ADDITIONAL_CA_BUNDLE_FILE)
    return context


def httpx_ssl_context(*ca_files: str | None) -> ssl.SSLContext:
    """Return HTTPX's normal trust extended with the provided CA bundles."""
    context = httpx.create_ssl_context()
    for ca_file in ca_files:
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
    return context


def httpx_additional_ca_ssl_context() -> ssl.SSLContext:
    """Return HTTPX's normal trust extended with the additional CA bundle."""
    return httpx_ssl_context(settings.ADDITIONAL_CA_BUNDLE_FILE)


def internal_httpx_ssl_context(*ca_files: str | None) -> ssl.SSLContext:
    """Return verified trust limited to explicit internal/operator CA bundles."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for ca_file in ca_files:
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
    return context


def httpx_verify(*, verify_tls: bool = True) -> bool | ssl.SSLContext:
    """Return an HTTPX verify value without weakening normal certificate checks."""
    if not verify_tls:
        return False
    if settings.ADDITIONAL_CA_BUNDLE_FILE:
        return httpx_additional_ca_ssl_context()
    return True


def httpx_tls_kwargs() -> dict[str, Any]:
    """Return HTTPX kwargs only when additional trust is configured."""
    return {"verify": httpx_verify()} if settings.ADDITIONAL_CA_BUNDLE_FILE else {}


def provider_http_client(provider: str) -> httpx.AsyncClient | None:
    """Return a pooled client preserving the selected provider SDK defaults."""
    if not settings.ADDITIONAL_CA_BUNDLE_FILE:
        return None
    normalized = provider.strip().lower()
    client = _provider_http_clients.get(normalized)
    if client is not None and not client.is_closed:
        return client

    client_kwargs = {"verify": httpx_additional_ca_ssl_context()}
    if normalized == "openai":
        client = OpenAIHttpClient(**client_kwargs)
    elif normalized == "anthropic":
        client = AnthropicHttpClient(**client_kwargs)
    elif normalized == "gemini":
        client = httpx.AsyncClient(follow_redirects=True, **client_kwargs)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    _provider_http_clients[normalized] = client
    return client


async def close_provider_http_clients() -> None:
    """Close pooled provider clients during application shutdown."""
    clients = list(_provider_http_clients.values())
    _provider_http_clients.clear()
    for client in clients:
        if not client.is_closed:
            await client.aclose()


def redis_tls_kwargs(redis_url: str) -> dict[str, Any]:
    """Apply the additional CA only when Redis TLS was explicitly selected."""
    if urlsplit(redis_url).scheme != "rediss" or not settings.ADDITIONAL_CA_BUNDLE_FILE:
        return {}
    return {
        "ssl_ca_certs": settings.ADDITIONAL_CA_BUNDLE_FILE,
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
    }


def sqlalchemy_connection_config(database_url: str) -> tuple[str, dict[str, Any]]:
    """Normalize asyncpg TLS options and add the custom CA when TLS is enabled."""
    parsed = urlsplit(database_url)
    if not parsed.scheme.startswith("postgresql") or not settings.ADDITIONAL_CA_BUNDLE_FILE:
        return database_url, {}

    query = parse_qsl(parsed.query, keep_blank_values=True)
    tls_values = [value.lower() for key, value in query if key in {"ssl", "sslmode"}]
    if not tls_values:
        return database_url, {}
    tls_mode = tls_values[-1]
    if tls_mode not in _TLS_ENABLED_VALUES | _TLS_DISABLED_VALUES:
        return database_url, {}

    normalized_query = [(key, value) for key, value in query if key not in {"ssl", "sslmode"}]
    normalized_url = urlunsplit(parsed._replace(query=urlencode(normalized_query)))
    if tls_mode in _TLS_DISABLED_VALUES:
        return normalized_url, {}
    return normalized_url, {"ssl": additional_ca_ssl_context()}
