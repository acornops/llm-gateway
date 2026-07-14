from __future__ import annotations

import ssl
from typing import Any

from app.config.settings import settings
from app.mcp.transports.http_transport import McpToolTransportError

BUILTIN_TOOL_TIMEOUT_HEADROOM_SECONDS = 5.0


def builtin_tool_transport_timeout_seconds(timeout_ms: int) -> float:
    """Leave enough transport headroom for the producer to return its own timeout result."""
    return max(timeout_ms / 1000.0, 0.001) + BUILTIN_TOOL_TIMEOUT_HEADROOM_SECONDS


def httpx_tls_kwargs() -> dict[str, Any]:
    if not settings.INTERNAL_TRANSPORT_TLS_ENABLED:
        return {}
    kwargs: dict[str, Any] = {
        "verify": settings.INTERNAL_TRANSPORT_TLS_CA_FILE,
    }
    if settings.INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT:
        kwargs["cert"] = (
            settings.INTERNAL_TRANSPORT_TLS_CERT_FILE,
            settings.INTERNAL_TRANSPORT_TLS_KEY_FILE,
        )
    return kwargs


def uvicorn_ssl_kwargs() -> dict[str, Any]:
    if not settings.INTERNAL_TRANSPORT_TLS_ENABLED:
        return {}
    kwargs: dict[str, Any] = {
        "ssl_certfile": settings.INTERNAL_TRANSPORT_TLS_CERT_FILE,
        "ssl_keyfile": settings.INTERNAL_TRANSPORT_TLS_KEY_FILE,
    }
    if settings.INTERNAL_TRANSPORT_TLS_CA_FILE:
        kwargs["ssl_ca_certs"] = settings.INTERNAL_TRANSPORT_TLS_CA_FILE
    if settings.INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT:
        kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    return kwargs


async def post_builtin_mcp_tool(
    url: str,
    name: str,
    arguments: dict[str, Any],
    timeout_ms: int,
    headers: dict[str, str],
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    import httpx

    async def post_bounded(
        client: httpx.AsyncClient, target_url: str, **kwargs: Any
    ) -> httpx.Response:
        async with client.stream("POST", target_url, **kwargs) as streamed:
            chunks: list[bytes] = []
            received = 0
            async for chunk in streamed.aiter_bytes():
                received += len(chunk)
                if received > settings.BUILTIN_MCP_MAX_RESPONSE_BYTES:
                    raise ValueError(
                        "Builtin MCP response exceeds the 3 MiB transport-envelope limit"
                    )
                chunks.append(chunk)
            return httpx.Response(
                streamed.status_code,
                headers=streamed.headers,
                content=b"".join(chunks),
                request=streamed.request,
            )

    transport_timeout = builtin_tool_transport_timeout_seconds(timeout_ms)
    async with httpx.AsyncClient(**httpx_tls_kwargs()) as client:
        response = await post_bounded(
            client,
            f"{url.rstrip('/')}/tools/call",
            json={
                "name": name,
                "arguments": arguments,
                **({"toolCallId": tool_call_id} if tool_call_id else {}),
            },
            timeout=transport_timeout,
            headers=headers,
            follow_redirects=False,
        )
        if response.status_code in (404, 405):
            response = await post_bounded(
                client,
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": "acornops-tools-call",
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": arguments,
                        **({"toolCallId": tool_call_id} if tool_call_id else {}),
                    },
                },
                timeout=transport_timeout,
                headers=headers,
                follow_redirects=False,
            )
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                message = error.get("message")
                public_message = (
                    message if isinstance(message, str) else "Builtin tool call failed."
                )
                outcome = error.get("outcome")
                return McpToolTransportError(
                    {
                        "isError": True,
                        "content": [{"type": "text", "text": public_message}],
                    },
                    code=error["code"],
                    dispatch_outcome=(
                        outcome if outcome in {"not_started", "unknown"} else "unknown"
                    ),
                    retryable=error.get("retryable") is True,
                )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            result = payload.get("result")
            return result if isinstance(result, dict) else {"isError": True, "content": []}
        return payload if isinstance(payload, dict) else {"isError": True, "content": []}
