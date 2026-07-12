from __future__ import annotations

import ssl
from typing import Any

from app.config.settings import settings


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

    async with httpx.AsyncClient(**httpx_tls_kwargs()) as client:
        response = await client.post(
            f"{url.rstrip('/')}/tools/call",
            json={
                "name": name,
                "arguments": arguments,
                **({"toolCallId": tool_call_id} if tool_call_id else {}),
            },
            timeout=timeout_ms / 1000.0,
            headers=headers,
            follow_redirects=False,
        )
        if response.status_code in (404, 405):
            response = await client.post(
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
                timeout=timeout_ms / 1000.0,
                headers=headers,
                follow_redirects=False,
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            result = payload.get("result")
            return result if isinstance(result, dict) else {"isError": True, "content": []}
        return payload if isinstance(payload, dict) else {"isError": True, "content": []}
