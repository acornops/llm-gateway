"""Bounded, DNS-pinned HTTP requests for MCP OAuth endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from app.config.settings import settings
from app.mcp.egress_policy import McpEgressPolicyError, prepare_mcp_egress_request
from app.mcp.oauth.errors import oauth_error
from app.outbound_tls import httpx_additional_ca_ssl_context


def validate_oauth_endpoint_url(url: str) -> None:
    """Reject URL forms that must never be used for OAuth protocol endpoints."""

    try:
        parsed = urlparse(url)
        _ = parsed.port
    except ValueError as exc:
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "The authorization server advertised an invalid endpoint.",
        ) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "The authorization server advertised an invalid endpoint.",
        )
    if parsed.username or parsed.password or parsed.fragment:
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "The authorization server advertised an unsafe endpoint.",
        )
    if len(url) > 4096:
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "The authorization server advertised an invalid endpoint.",
        )
    runtime_env = (settings.NODE_ENV or settings.APP_ENV).strip().lower()
    if runtime_env == "production" and parsed.scheme != "https":
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "OAuth endpoints must use HTTPS.",
        )


async def validate_oauth_endpoint_egress(url: str) -> None:
    validate_oauth_endpoint_url(url)
    try:
        await prepare_mcp_egress_request(url)
    except McpEgressPolicyError as exc:
        raise oauth_error(
            "MCP_OAUTH_EGRESS_BLOCKED",
            "The OAuth endpoint is blocked by outbound network policy.",
            status_code=409,
        ) from exc


async def oauth_http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: object | None = None,
    form_body: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Perform one isolated OAuth request with bounded response buffering."""

    validate_oauth_endpoint_url(url)
    try:
        target = await prepare_mcp_egress_request(url)
    except McpEgressPolicyError as exc:
        raise oauth_error(
            "MCP_OAUTH_EGRESS_BLOCKED",
            "The OAuth endpoint is blocked by outbound network policy.",
            status_code=409,
        ) from exc
    request_headers = dict(headers or {})
    # These transport-security headers are derived locally and must not be
    # replaceable by any higher-level OAuth request builder.
    request_headers["host"] = target.host_header
    request_headers["accept-encoding"] = "identity"
    transport = httpx.AsyncHTTPTransport(verify=httpx_additional_ca_ssl_context())
    timeout_seconds = max(settings.MCP_OAUTH_HTTP_TIMEOUT_MS / 1000.0, 0.001)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    ) as client:
        request = client.build_request(
            method,
            target.connection_url,
            headers=request_headers,
            json=json_body,
            data=form_body,
        )
        request.extensions.update(target.extensions)
        try:
            response = await client.send(request, stream=True)
        except httpx.TransportError as exc:
            raise oauth_error(
                "MCP_OAUTH_ENDPOINT_OUTCOME_UNKNOWN",
                "The OAuth endpoint could not be reached safely.",
                status_code=503,
                retryable=True,
            ) from exc
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            await response.aclose()
            raise oauth_error(
                "MCP_OAUTH_RESPONSE_INVALID",
                "The authorization server returned an unsupported response encoding.",
            )
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except ValueError:
                await response.aclose()
                raise oauth_error(
                    "MCP_OAUTH_RESPONSE_INVALID",
                    "The authorization server returned an invalid response length.",
                ) from None
            if parsed_length < 0 or parsed_length > settings.MCP_OAUTH_MAX_RESPONSE_BYTES:
                await response.aclose()
                raise oauth_error(
                    (
                        "MCP_OAUTH_RESPONSE_TOO_LARGE"
                        if parsed_length >= 0
                        else "MCP_OAUTH_RESPONSE_INVALID"
                    ),
                    (
                        "The authorization server response exceeded the configured limit."
                        if parsed_length >= 0
                        else "The authorization server returned an invalid response length."
                    ),
                )
        body = bytearray()
        try:
            async for chunk in response.aiter_raw():
                body.extend(chunk)
                if len(body) > settings.MCP_OAUTH_MAX_RESPONSE_BYTES:
                    await response.aclose()
                    raise oauth_error(
                        "MCP_OAUTH_RESPONSE_TOO_LARGE",
                        "The authorization server response exceeded the configured limit.",
                    )
        except httpx.TransportError as exc:
            await response.aclose()
            raise oauth_error(
                "MCP_OAUTH_ENDPOINT_OUTCOME_UNKNOWN",
                "The OAuth endpoint response could not be read safely.",
                status_code=503,
                retryable=True,
            ) from exc
        await response.aclose()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=httpx.Request(method, url, headers=dict(headers or {})),
        )
