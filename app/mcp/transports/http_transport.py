"""Standards-compliant MCP Streamable HTTP client transport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
import structlog
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from app.config.settings import settings
from app.mcp.egress_policy import (
    McpEgressPolicyError,
    ValidatedMcpRequestTarget,
    prepare_mcp_egress_request,
)
from app.mcp.header_policy import MCP_TRANSPORT_HEADER_NAMES
from app.resilience.outbound import (
    CircuitOpenError,
    backoff_seconds,
    dependency_circuit_breaker,
    is_retryable_dependency_error,
    note_dependency_event,
)

logger = structlog.get_logger()

# The SDK's v1 transport logs issued session IDs at INFO. AcornOps owns remote
# transport telemetry and never emits session IDs or raw MCP messages.
_sdk_transport_logger = logging.getLogger("mcp.client.streamable_http")
_sdk_transport_logger.handlers = [logging.NullHandler()]
_sdk_transport_logger.propagate = False

_CLIENT_INFO = types.Implementation(
    name="acornops-llm-gateway",
    version="0.0.1-experimental.3",
)
_MAX_ERROR_BODY_BYTES = 4096
_MAX_DISCOVERY_PAGES = 100
_SESSION_TERMINATED_ERROR_CODE = 32600
_SESSION_TERMINATED_ERROR_MESSAGE = "Session terminated"

T = TypeVar("T")
TransportFactory = Callable[[], httpx.AsyncBaseTransport]
SessionOperation = Callable[[ClientSession], Awaitable[T]]


def _default_transport_factory() -> httpx.AsyncBaseTransport:
    """Extend normal HTTPX trust only for generic remote MCP traffic."""
    try:
        ssl_context = httpx.create_ssl_context()
        ca_bundle_file = settings.MCP_EGRESS_CA_BUNDLE_FILE.strip()
        if ca_bundle_file:
            ssl_context.load_verify_locations(cafile=ca_bundle_file)
    except OSError as error:
        raise McpEgressPolicyError("MCP egress CA bundle could not be loaded") from error
    return httpx.AsyncHTTPTransport(verify=ssl_context)


class McpToolTransportError(dict[str, Any]):
    """Sanitized MCP error carrying trusted local dispatch semantics."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        code: str,
        dispatch_outcome: str,
        retryable: bool,
    ) -> None:
        super().__init__(payload)
        self.code = code
        self.dispatch_outcome = dispatch_outcome
        self.retryable = retryable


class McpResponseTooLargeError(ValueError):
    """Raised before an MCP peer can exceed the transport response ceiling."""


class McpResponseEncodingError(ValueError):
    """Raised when a peer ignores the required identity response encoding."""


class McpProtocolError(ValueError):
    """Raised for an invalid or unsupported MCP protocol response."""


class _BoundedResponseStream(httpx.AsyncByteStream):
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


class _BoundedAsyncTransport(httpx.AsyncBaseTransport):
    """Wrap an HTTPX transport with a per-response byte ceiling."""

    def __init__(self, transport: httpx.AsyncBaseTransport, max_bytes: int) -> None:
        self._transport = transport
        self._max_bytes = max_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        response.stream = _BoundedResponseStream(response.stream, self._max_bytes)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def _exception_members(error: BaseException) -> list[BaseException]:
    members: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
        else:
            members.append(current)
    return members


def _primary_exception(error: BaseException) -> BaseException:
    members = _exception_members(error)
    priorities = (
        McpResponseTooLargeError,
        McpResponseEncodingError,
        httpx.TimeoutException,
        httpx.HTTPStatusError,
        httpx.RequestError,
        McpError,
        McpProtocolError,
    )
    for error_type in priorities:
        for member in members:
            if isinstance(member, error_type):
                return member
    return members[0] if members else error


def _is_session_terminated(error: BaseException) -> bool:
    for member in _exception_members(error):
        if not isinstance(member, McpError):
            continue
        if (
            member.error.code == _SESSION_TERMINATED_ERROR_CODE
            and member.error.message == _SESSION_TERMINATED_ERROR_MESSAGE
        ):
            return True
    return False


def _redact_error_text(text: str, request: httpx.Request) -> str | None:
    sanitized = " ".join(text.split())
    for value in sorted(
        {value for value in request.headers.values() if value},
        key=len,
        reverse=True,
    ):
        sanitized = sanitized.replace(value, "[REDACTED]")
    sanitized = "".join(character for character in sanitized if character.isprintable())
    return sanitized[:500] or None


def _extract_upstream_error(body: bytes, request: httpx.Request) -> str | None:
    text: str | None = None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = body.decode("utf-8", errors="replace")
    else:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                text = error["message"]
            elif isinstance(payload.get("detail"), str):
                text = payload["detail"]
    return _redact_error_text(text, request) if text else None


def _json_size(payload: Any) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _ensure_payload_size(payload: Any) -> None:
    if _json_size(payload) > settings.MCP_MAX_TOOL_RESULT_BYTES:
        raise McpResponseTooLargeError("MCP response exceeds the 2 MiB result limit")


def _loggable_server_origin(url: str) -> str:
    """Return a credential-, path-, query-, and fragment-free server origin."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "[invalid MCP server URL]"
    if parsed.scheme not in {"http", "https"} or not hostname:
        return "[invalid MCP server URL]"
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{rendered_host}{rendered_port}"


class McpHttpTransport:
    """Execute generic remote MCP operations over standard Streamable HTTP."""

    def __init__(self, transport_factory: TransportFactory | None = None) -> None:
        self._transport_factory = transport_factory or _default_transport_factory

    @staticmethod
    def _error_payload(prefix: str, detail: str | None = None) -> dict[str, Any]:
        message = f"{prefix}: {detail}" if detail else prefix
        return {
            "isError": True,
            "content": [{"type": "text", "text": message}],
        }

    @classmethod
    def _transport_error_payload(
        cls,
        prefix: str,
        *,
        code: str,
        dispatch_outcome: str,
        retryable: bool,
        detail: str | None = None,
    ) -> McpToolTransportError:
        return McpToolTransportError(
            cls._error_payload(prefix, detail),
            code=code,
            dispatch_outcome=dispatch_outcome,
            retryable=retryable,
        )

    async def close(self) -> None:
        """Retained for application-lifespan compatibility; sessions are per operation."""

    async def _observe_response(self, response: httpx.Response) -> None:
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding not in ("", "identity"):
            await response.aclose()
            raise McpResponseEncodingError(
                "MCP server returned a compressed response despite Accept-Encoding: identity"
            )
        if response.status_code < 400:
            return
        if response.status_code == 405 and response.request.method in ("GET", "DELETE"):
            return

        body = bytearray()
        truncated = False
        if response.is_stream_consumed:
            body.extend(response.content[: _MAX_ERROR_BODY_BYTES + 1])
            truncated = len(body) > _MAX_ERROR_BODY_BYTES
        else:
            async for chunk in response.aiter_raw():
                remaining = _MAX_ERROR_BODY_BYTES + 1 - len(body)
                body.extend(chunk[:remaining])
                if len(body) > _MAX_ERROR_BODY_BYTES:
                    truncated = True
                    break
        del body[_MAX_ERROR_BODY_BYTES:]
        await response.aclose()
        logger.warning(
            "mcp_upstream_http_error",
            exception_type="HTTPStatusError",
            status_code=response.status_code,
            upstream_error=_extract_upstream_error(bytes(body), response.request),
            upstream_error_truncated=truncated,
        )

    def _http_client(
        self,
        target: ValidatedMcpRequestTarget,
        headers: dict[str, str] | None,
        timeout_seconds: float,
    ) -> httpx.AsyncClient:
        request_headers = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() not in MCP_TRANSPORT_HEADER_NAMES
        }
        request_headers.update(
            {
                "host": target.host_header,
                "accept-encoding": "identity",
            }
        )

        async def apply_pinned_tls(request: httpx.Request) -> None:
            request.extensions.update(target.extensions)

        transport = _BoundedAsyncTransport(
            self._transport_factory(), settings.MCP_MAX_TOOL_RESULT_BYTES
        )
        return httpx.AsyncClient(
            headers=request_headers,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            event_hooks={
                "request": [apply_pinned_tls],
                "response": [self._observe_response],
            },
        )

    async def _run_session_once(
        self,
        target: ValidatedMcpRequestTarget,
        timeout_ms: int,
        headers: dict[str, str] | None,
        operation: SessionOperation[T],
    ) -> T:
        timeout_seconds = max(timeout_ms / 1000.0, 0.001)
        async with (
            self._http_client(target, headers, timeout_seconds) as client,
            streamable_http_client(
                target.connection_url,
                http_client=client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _get_session_id),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
                client_info=_CLIENT_INFO,
            ) as session,
        ):
            try:
                initialization = await session.initialize()
            except RuntimeError as error:
                raise McpProtocolError(
                    "MCP protocol-version negotiation failed"
                ) from error
            if initialization.capabilities.tools is None:
                raise McpProtocolError(
                    "MCP server did not advertise the tools capability"
                )
            return await operation(session)

    async def _run_session(
        self,
        target: ValidatedMcpRequestTarget,
        timeout_ms: int,
        headers: dict[str, str] | None,
        operation: SessionOperation[T],
        *,
        reinitialize_on_session_termination: bool = False,
    ) -> T:
        try:
            return await self._run_session_once(target, timeout_ms, headers, operation)
        except Exception as error:
            if not (
                reinitialize_on_session_termination and _is_session_terminated(error)
            ):
                raise
        logger.info(
            "mcp_session_reinitializing",
            server_url=_loggable_server_origin(target.original_url),
        )
        return await self._run_session_once(target, timeout_ms, headers, operation)

    async def call_tool(
        self,
        url: str,
        name: str,
        arguments: dict[str, Any],
        timeout_ms: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Initialize a remote MCP session and execute one tools/call request."""
        dependency_key = f"mcp:call:{url.rstrip('/')}"
        try:
            target = await prepare_mcp_egress_request(url)
            await dependency_circuit_breaker.before_call(dependency_key, "mcp", url)

            async def call(session: ClientSession) -> types.CallToolResult:
                # ClientSession.call_tool() performs a tools/list request after a
                # successful call to validate output schemas. The gateway already
                # validates against the registry schema, and a post-call discovery
                # failure would otherwise turn a completed side effect into an
                # ambiguous transport error.
                return await session.send_request(
                    types.ClientRequest(
                        types.CallToolRequest(
                            params=types.CallToolRequestParams(
                                name=name,
                                arguments=arguments,
                            )
                        )
                    ),
                    types.CallToolResult,
                    request_read_timeout_seconds=timedelta(
                        seconds=max(timeout_ms / 1000.0, 0.001)
                    ),
                )

            result = await self._run_session(target, timeout_ms, headers, call)
            payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
            _ensure_payload_size(payload)
            await dependency_circuit_breaker.record_success(dependency_key)
            return payload
        except CircuitOpenError:
            return self._transport_error_payload(
                "MCP server unavailable",
                code="MCP_CIRCUIT_OPEN",
                dispatch_outcome="not_started",
                retryable=True,
            )
        except McpEgressPolicyError as error:
            logger.warning(
                "mcp_tool_call_egress_policy_failed",
                server_url=_loggable_server_origin(url),
                tool=name,
                exception_type=error.__class__.__name__,
            )
            return self._transport_error_payload(
                "MCP server blocked by egress policy",
                code="MCP_EGRESS_BLOCKED",
                dispatch_outcome="not_started",
                retryable=False,
            )
        except Exception as error:
            primary = _primary_exception(error)
            retryable = is_retryable_dependency_error(primary)
            logger.warning(
                "mcp_tool_call_failed",
                server_url=_loggable_server_origin(url),
                tool=name,
                exception_type=primary.__class__.__name__,
                status_code=getattr(getattr(primary, "response", None), "status_code", None),
            )
            note_dependency_event("mcp", "failure")
            if retryable:
                opened = await dependency_circuit_breaker.record_failure(
                    dependency_key,
                    settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                    settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
                )
                if opened:
                    note_dependency_event("mcp", "circuit_open")
            return self._transport_error_payload(
                self._tool_error_prefix(primary),
                code=self._tool_error_code(primary),
                dispatch_outcome="unknown",
                retryable=retryable,
            )

    async def list_tools(
        self,
        url: str,
        timeout_ms: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Initialize a remote MCP session and discover all tools/list pages."""
        try:
            target = await prepare_mcp_egress_request(url)
        except McpEgressPolicyError as error:
            logger.warning(
                "mcp_tool_discovery_egress_policy_failed",
                server_url=_loggable_server_origin(url),
                exception_type=error.__class__.__name__,
            )
            return self._error_payload("MCP server blocked by egress policy")

        dependency_key = f"mcp:discovery:{url.rstrip('/')}"
        attempts = max(1, settings.MCP_DISCOVERY_RETRY_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            try:
                await dependency_circuit_breaker.before_call(dependency_key, "mcp", url)

                async def discover(session: ClientSession) -> dict[str, Any]:
                    tools: list[dict[str, Any]] = []
                    cursor: str | None = None
                    seen_cursors: set[str] = set()
                    for _page in range(_MAX_DISCOVERY_PAGES):
                        page = await session.list_tools(cursor=cursor)
                        tools.extend(
                            tool.model_dump(
                                by_alias=True, mode="json", exclude_none=True
                            )
                            for tool in page.tools
                        )
                        payload = {"tools": tools}
                        _ensure_payload_size(payload)
                        cursor = page.nextCursor
                        if cursor is None:
                            return payload
                        if cursor in seen_cursors:
                            raise McpProtocolError(
                                "MCP server repeated a tools/list pagination cursor"
                            )
                        seen_cursors.add(cursor)
                    raise McpProtocolError(
                        "MCP server exceeded the tools/list pagination limit"
                    )

                payload = await self._run_session(
                    target,
                    timeout_ms,
                    headers,
                    discover,
                    reinitialize_on_session_termination=True,
                )
                await dependency_circuit_breaker.record_success(dependency_key)
                return payload
            except CircuitOpenError:
                return self._error_payload("MCP server unavailable")
            except Exception as error:
                primary = _primary_exception(error)
                retryable = is_retryable_dependency_error(primary)
                logger.warning(
                    "mcp_tool_discovery_attempt_failed",
                    server_url=_loggable_server_origin(url),
                    attempt=attempt,
                    max_attempts=attempts,
                    exception_type=primary.__class__.__name__,
                    status_code=getattr(
                        getattr(primary, "response", None), "status_code", None
                    ),
                )
                note_dependency_event("mcp", "failure")
                if retryable:
                    opened = await dependency_circuit_breaker.record_failure(
                        dependency_key,
                        settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                        settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
                    )
                    if opened:
                        note_dependency_event("mcp", "circuit_open")
                    if attempt < attempts and not opened:
                        note_dependency_event("mcp", "retry")
                        await asyncio.sleep(
                            backoff_seconds(
                                settings.MCP_DISCOVERY_RETRY_BACKOFF_MS, attempt
                            )
                        )
                        continue
                return self._error_payload(self._discovery_error_prefix(primary))
        return self._error_payload("MCP server tool discovery failed")

    @staticmethod
    def _tool_error_prefix(error: BaseException) -> str:
        if isinstance(error, httpx.TimeoutException):
            return "MCP server timeout"
        if isinstance(error, McpResponseTooLargeError):
            return "MCP response exceeds the result limit"
        if isinstance(error, (httpx.RequestError, httpx.HTTPStatusError)):
            return "Failed to connect to MCP server"
        if isinstance(error, (McpError, McpProtocolError, McpResponseEncodingError)):
            return "MCP protocol error"
        return "Upstream tool error"

    @staticmethod
    def _tool_error_code(error: BaseException) -> str:
        if isinstance(error, httpx.TimeoutException):
            return "MCP_TOOL_TIMEOUT"
        if isinstance(error, McpResponseTooLargeError):
            return "MCP_RESULT_TOO_LARGE"
        if isinstance(error, (httpx.RequestError, httpx.HTTPStatusError)):
            return "MCP_TOOL_REQUEST_FAILED"
        if isinstance(error, (McpError, McpProtocolError, McpResponseEncodingError)):
            return "MCP_PROTOCOL_ERROR"
        return "MCP_TOOL_TRANSPORT_FAILED"

    @staticmethod
    def _discovery_error_prefix(error: BaseException) -> str:
        if isinstance(error, McpEgressPolicyError):
            return "MCP server blocked by egress policy"
        if isinstance(error, httpx.TimeoutException):
            return "MCP server discovery timeout"
        if isinstance(error, McpResponseTooLargeError):
            return "MCP server discovery response exceeds the result limit"
        if isinstance(error, (httpx.RequestError, httpx.HTTPStatusError)):
            return "Failed to connect to MCP server"
        if isinstance(error, (McpError, McpProtocolError, McpResponseEncodingError)):
            return "MCP server protocol error"
        return "Upstream tool discovery error"


mcp_transport = McpHttpTransport()
