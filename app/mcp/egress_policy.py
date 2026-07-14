from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from app.config.settings import settings


class McpEgressPolicyError(ValueError):
    pass


@dataclass
class _DnsCacheEntry:
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
    expires_at_monotonic: float


@dataclass(frozen=True)
class ValidatedMcpRequestTarget:
    original_url: str
    connection_url: str
    host_header: str
    extensions: dict[str, str]


_dns_cache: dict[str, _DnsCacheEntry] = {}


def _runtime_env() -> str:
    return (settings.NODE_ENV or settings.APP_ENV).strip().lower()


def _allowed_hosts() -> set[str]:
    return {
        host.strip().lower()
        for host in settings.MCP_EGRESS_ALLOWED_HOSTS.split(",")
        if host.strip()
    }


def _is_local_development_host(hostname: str) -> bool:
    return (
        _runtime_env() != "production"
        and (
            hostname == "localhost"
            or hostname.endswith(".localhost")
            or "." not in hostname
        )
    )


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    )


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _host_for_url(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{address.compressed}]"
    return str(address)


def _host_for_header(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
        if isinstance(address, ipaddress.IPv6Address):
            return f"[{address.compressed}]"
    except ValueError:
        pass
    return hostname


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = _host_for_header(hostname)
    if port == _default_port(scheme):
        return host
    return f"{host}:{port}"


def _url_with_host(parsed, host: str, port: int) -> str:
    netloc = host if port == _default_port(parsed.scheme) else f"{host}:{port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


async def _resolve_host(
    hostname: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    cached = _dns_cache.get(hostname)
    if cached and cached.expires_at_monotonic > time.monotonic():
        return cached.addresses

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except socket.gaierror as exc:
        raise McpEgressPolicyError(f"MCP server host {hostname} could not be resolved") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        raw_address = info[4][0]
        if raw_address in seen:
            continue
        seen.add(raw_address)
        addresses.append(ipaddress.ip_address(raw_address))

    if not addresses:
        raise McpEgressPolicyError(f"MCP server host {hostname} did not resolve to any IPs")

    _dns_cache[hostname] = _DnsCacheEntry(
        addresses=addresses,
        expires_at_monotonic=time.monotonic() + settings.MCP_EGRESS_DNS_CACHE_TTL_SEC,
    )
    return addresses


async def prepare_mcp_egress_request(url: str) -> ValidatedMcpRequestTarget:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port or _default_port(parsed.scheme)
    except ValueError as exc:
        raise McpEgressPolicyError("MCP server URL has an invalid host or port") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise McpEgressPolicyError("MCP server URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise McpEgressPolicyError("MCP server URL must not include credentials")

    allowed_hosts = _allowed_hosts()
    host_is_allowed = hostname in allowed_hosts
    local_development_host = _is_local_development_host(hostname)
    if (
        settings.MCP_EGRESS_REQUIRE_HTTPS
        and parsed.scheme != "https"
        and _runtime_env() == "production"
        and not local_development_host
    ):
        raise McpEgressPolicyError("Remote MCP server URLs must use HTTPS")

    try:
        literal_address = ipaddress.ip_address(hostname)
        addresses = [literal_address]
        hostname_is_literal = True
    except ValueError:
        if _runtime_env() != "production":
            return ValidatedMcpRequestTarget(
                original_url=url,
                connection_url=url,
                host_header=_host_header(
                    hostname,
                    port,
                    parsed.scheme,
                ),
                extensions={},
            )
        addresses = await _resolve_host(hostname)
        hostname_is_literal = False

    if not host_is_allowed and not settings.MCP_EGRESS_ALLOW_PRIVATE_NETWORKS:
        local_addresses_allowed = settings.MCP_EGRESS_ALLOW_LOCAL_ADDRESSES and all(
            address.is_loopback or address.is_link_local for address in addresses
        )
        if not local_addresses_allowed:
            blocked = [str(address) for address in addresses if _is_blocked_address(address)]
            if blocked:
                raise McpEgressPolicyError(
                    "MCP server URL resolves to a blocked private, local, or reserved address"
                )

    host_header = _host_header(hostname, port, parsed.scheme)
    if hostname_is_literal or _runtime_env() != "production":
        return ValidatedMcpRequestTarget(
            original_url=url,
            connection_url=url,
            host_header=host_header,
            extensions={},
        )

    pinned_address = addresses[0]
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
    return ValidatedMcpRequestTarget(
        original_url=url,
        connection_url=_url_with_host(parsed, _host_for_url(pinned_address), port),
        host_header=host_header,
        extensions=extensions,
    )


async def validate_mcp_server_url(url: str) -> None:
    await prepare_mcp_egress_request(url)
