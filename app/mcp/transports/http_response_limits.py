"""Response-size enforcement for the remote MCP HTTP transport."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.mcp.egress_policy import McpEgressPolicyError
from app.outbound_tls import httpx_additional_ca_ssl_context


class McpResponseTooLargeError(ValueError):
    """Raised before an MCP peer can exceed the transport response ceiling."""


class McpResponseEncodingError(ValueError):
    """Raised when a peer ignores the required identity response encoding."""


class BoundedResponseStream(httpx.AsyncByteStream):
    """Limit raw response bytes before HTTPX or the MCP SDK buffers them."""

    def __init__(self, stream: httpx.AsyncByteStream, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._received = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._received += len(chunk)
            if self._received > self._max_bytes:
                await self._stream.aclose()
                raise McpResponseTooLargeError(
                    "MCP response exceeds the 2 MiB result limit"
                )
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class BoundedAsyncTransport(httpx.AsyncBaseTransport):
    """Wrap an HTTPX transport with a per-response byte ceiling."""

    def __init__(self, transport: httpx.AsyncBaseTransport, max_bytes: int) -> None:
        self._transport = transport
        self._max_bytes = max_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                await response.aclose()
                raise McpResponseEncodingError(
                    "MCP response contains an invalid Content-Length header"
                ) from None
            if declared_bytes < 0:
                await response.aclose()
                raise McpResponseEncodingError(
                    "MCP response contains an invalid Content-Length header"
                )
            if declared_bytes > self._max_bytes:
                await response.aclose()
                raise McpResponseTooLargeError(
                    "MCP response exceeds the 2 MiB result limit"
                )
        response.stream = BoundedResponseStream(response.stream, self._max_bytes)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def default_transport_factory() -> httpx.AsyncBaseTransport:
    """Use the shared additive trust configuration for remote MCP traffic."""

    try:
        ssl_context = httpx_additional_ca_ssl_context()
    except OSError as error:
        raise McpEgressPolicyError(
            "Additional CA bundle could not be loaded"
        ) from error
    return httpx.AsyncHTTPTransport(verify=ssl_context)
