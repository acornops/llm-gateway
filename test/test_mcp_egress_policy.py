import socket

import pytest

from app.mcp import egress_policy
from app.mcp.egress_policy import (
    McpEgressPolicyError,
    prepare_mcp_egress_request,
    validate_mcp_server_url,
)


@pytest.fixture(autouse=True)
def reset_egress_settings(monkeypatch: pytest.MonkeyPatch):
    egress_policy._dns_cache.clear()
    monkeypatch.setattr("app.mcp.egress_policy.settings.APP_ENV", "production")
    monkeypatch.setattr("app.mcp.egress_policy.settings.NODE_ENV", None)
    monkeypatch.setattr("app.mcp.egress_policy.settings.MCP_EGRESS_REQUIRE_HTTPS", True)
    monkeypatch.setattr("app.mcp.egress_policy.settings.MCP_EGRESS_ALLOW_PRIVATE_NETWORKS", False)
    monkeypatch.setattr("app.mcp.egress_policy.settings.MCP_EGRESS_ALLOW_LOCAL_ADDRESSES", False)
    monkeypatch.setattr("app.mcp.egress_policy.settings.MCP_EGRESS_ALLOWED_HOSTS", "")
    monkeypatch.setattr("app.mcp.egress_policy.settings.MCP_EGRESS_DNS_CACHE_TTL_SEC", 300)


def _fake_getaddrinfo(address: str):
    def fake_getaddrinfo(host: str, _port: int | None):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (address, 443),
            )
        ]

    return fake_getaddrinfo


@pytest.mark.anyio
async def test_rejects_remote_http_url_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.mcp.egress_policy.socket.getaddrinfo",
        _fake_getaddrinfo("93.184.216.34"),
    )

    with pytest.raises(McpEgressPolicyError, match="HTTPS"):
        await validate_mcp_server_url("http://mcp.example.com")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    (
        "https://mcp.example.com:not-a-port/mcp",
        "https://mcp.example.com:70000/mcp",
    ),
)
async def test_rejects_invalid_ports_as_egress_policy_errors(url: str):
    with pytest.raises(McpEgressPolicyError, match="invalid host or port"):
        await validate_mcp_server_url(url)


@pytest.mark.anyio
async def test_rejects_private_resolved_addresses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.mcp.egress_policy.socket.getaddrinfo",
        _fake_getaddrinfo("10.1.2.3"),
    )

    with pytest.raises(McpEgressPolicyError, match="blocked private"):
        await validate_mcp_server_url("https://mcp.example.com")


@pytest.mark.anyio
async def test_allows_public_https_addresses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.mcp.egress_policy.socket.getaddrinfo",
        _fake_getaddrinfo("93.184.216.34"),
    )

    await validate_mcp_server_url("https://mcp.example.com")


@pytest.mark.anyio
async def test_pins_public_dns_resolution_into_connection_target(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.mcp.egress_policy.socket.getaddrinfo",
        _fake_getaddrinfo("93.184.216.34"),
    )

    target = await prepare_mcp_egress_request("https://mcp.example.com:8443/root")

    assert target.original_url == "https://mcp.example.com:8443/root"
    assert target.connection_url == "https://93.184.216.34:8443/root"
    assert target.host_header == "mcp.example.com:8443"
    assert target.extensions == {"sni_hostname": "mcp.example.com"}


@pytest.mark.anyio
async def test_allows_local_development_hosts_without_dns():
    egress_policy._dns_cache.clear()
    egress_policy.settings.APP_ENV = "development"

    await validate_mcp_server_url("http://mock-mcp:8000")


@pytest.mark.anyio
async def test_explicit_https_allowed_host_bypasses_private_address_block(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("app.mcp.egress_policy.settings.MCP_EGRESS_ALLOWED_HOSTS", "mcp.internal")
    monkeypatch.setattr(
        "app.mcp.egress_policy.socket.getaddrinfo",
        _fake_getaddrinfo("10.1.2.3"),
    )

    await validate_mcp_server_url("https://mcp.internal")


@pytest.mark.anyio
async def test_explicit_allowed_host_does_not_bypass_https_requirement(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.mcp.egress_policy.settings.MCP_EGRESS_ALLOWED_HOSTS", "mcp.internal"
    )

    with pytest.raises(McpEgressPolicyError, match="HTTPS"):
        await validate_mcp_server_url("http://mcp.internal")
