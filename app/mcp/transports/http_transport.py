"""Standards-compliant MCP Streamable HTTP client transport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

import httpx
import structlog
from mcp import ClientSession, types
from mcp.client.auth.utils import extract_field_from_www_auth, extract_scope_from_www_auth
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from app.config.settings import settings
from app.mcp.egress_policy import (
    McpEgressPolicyError,
    ValidatedMcpRequestTarget,
    prepare_mcp_egress_request,
)
from app.mcp.header_policy import MCP_TRANSPORT_HEADER_NAMES
from app.mcp.logging import loggable_mcp_server_origin
from app.mcp.transports.http_response_limits import (
    BoundedAsyncTransport as _BoundedAsyncTransport,
)
from app.mcp.transports.http_response_limits import (
    McpResponseEncodingError,
    McpResponseTooLargeError,
)
from app.mcp.transports.http_response_limits import (
    default_transport_factory as _default_transport_factory,
)
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


class McpToolTransportError(dict[str, Any]):
    """Sanitized MCP error carrying trusted local dispatch semantics."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        code: str,
        dispatch_outcome: str,
        retryable: bool,
        auth_error: str | None = None,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(payload)
        self.code = code
        self.dispatch_outcome = dispatch_outcome
        self.retryable = retryable
        self.auth_error = auth_error
        self.required_scopes = required_scopes or []


class McpProtocolError(ValueError):
    """Raised for an invalid or unsupported MCP protocol response."""


class McpEndpointNotFoundError(ValueError):
    """Raised when a fresh authenticated MCP session repeatedly returns 404."""


class McpAuthenticationError(ValueError):
    """Raised when an MCP peer rejects the installation credential."""

    def __init__(
        self,
        status_code: int,
        *,
        auth_error: str | None = None,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__("MCP server rejected the configured credential")
        self.status_code = status_code
        self.auth_error = auth_error
        self.required_scopes = required_scopes or []


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
        McpAuthenticationError,
        McpEndpointNotFoundError,
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


def _json_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _ensure_payload_size(payload: Any) -> None:
    if _json_size(payload) > settings.MCP_MAX_TOOL_RESULT_BYTES:
        raise McpResponseTooLargeError("MCP response exceeds the 2 MiB result limit")


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
        auth_error: str | None = None,
        required_scopes: list[str] | None = None,
    ) -> McpToolTransportError:
        return McpToolTransportError(
            cls._error_payload(prefix, detail),
            code=code,
            dispatch_outcome=dispatch_outcome,
            retryable=retryable,
            auth_error=auth_error,
            required_scopes=required_scopes,
        )

    async def _observe_response(self, response: httpx.Response) -> None:
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding not in ("", "identity"):
            await response.aclose()
            raise McpResponseEncodingError(
                "MCP server returned a compressed response despite Accept-Encoding: identity"
            )
        if response.status_code < 400:
            return
        method = response.request.method
        if response.status_code == 405 and method in ("GET", "DELETE"):
            return
        # Session termination is idempotent. Some compliant remote peers return
        # 404 when the session is already absent instead of 204 or 405.
        if response.status_code == 404 and method == "DELETE":
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
            upstream_method=response.request.method,
            upstream_error_truncated=truncated,
        )
        if response.status_code in (401, 403):
            raise McpAuthenticationError(
                response.status_code,
                auth_error=extract_field_from_www_auth(response, "error"),
                required_scopes=(extract_scope_from_www_auth(response) or "").split(),
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
                raise McpProtocolError("MCP protocol-version negotiation failed") from error
            if initialization.capabilities.tools is None:
                raise McpProtocolError("MCP server did not advertise the tools capability")
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
            if not (reinitialize_on_session_termination and _is_session_terminated(error)):
                raise
        logger.info(
            "mcp_session_reinitializing",
            server_url=loggable_mcp_server_origin(target.original_url),
        )
        try:
            return await self._run_session_once(target, timeout_ms, headers, operation)
        except Exception as error:
            if _is_session_terminated(error):
                raise McpEndpointNotFoundError(
                    "MCP endpoint returned Not Found for a fresh session"
                ) from error
            raise

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
                    request_read_timeout_seconds=timedelta(seconds=max(timeout_ms / 1000.0, 0.001)),
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
                server_url=loggable_mcp_server_origin(url),
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
                server_url=loggable_mcp_server_origin(url),
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
                auth_error=(
                    primary.auth_error if isinstance(primary, McpAuthenticationError) else None
                ),
                required_scopes=(
                    primary.required_scopes if isinstance(primary, McpAuthenticationError) else None
                ),
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
                server_url=loggable_mcp_server_origin(url),
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
                            tool.model_dump(by_alias=True, mode="json", exclude_none=True)
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
                    raise McpProtocolError("MCP server exceeded the tools/list pagination limit")

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
                return self._transport_error_payload(
                    "MCP server unavailable",
                    code="MCP_ENDPOINT_UNAVAILABLE",
                    dispatch_outcome="not_started",
                    retryable=True,
                )
            except Exception as error:
                primary = _primary_exception(error)
                retryable = is_retryable_dependency_error(primary)
                logger.warning(
                    "mcp_tool_discovery_attempt_failed",
                    server_url=loggable_mcp_server_origin(url),
                    attempt=attempt,
                    max_attempts=attempts,
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
                    if attempt < attempts and not opened:
                        note_dependency_event("mcp", "retry")
                        await asyncio.sleep(
                            backoff_seconds(settings.MCP_DISCOVERY_RETRY_BACKOFF_MS, attempt)
                        )
                        continue
                return self._transport_error_payload(
                    self._discovery_error_prefix(primary),
                    code=self._discovery_error_code(primary),
                    dispatch_outcome="not_started",
                    retryable=retryable,
                    auth_error=(
                        primary.auth_error if isinstance(primary, McpAuthenticationError) else None
                    ),
                    required_scopes=(
                        primary.required_scopes
                        if isinstance(primary, McpAuthenticationError)
                        else None
                    ),
                )
        return self._transport_error_payload(
            "MCP server tool discovery failed",
            code="MCP_TOOL_DISCOVERY_FAILED",
            dispatch_outcome="not_started",
            retryable=False,
        )

    @staticmethod
    def _tool_error_prefix(error: BaseException) -> str:
        if isinstance(error, McpAuthenticationError):
            return "MCP server rejected the configured credential"
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
        if isinstance(error, McpAuthenticationError):
            return "MCP_AUTHENTICATION_FAILED"
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
        if isinstance(error, McpAuthenticationError):
            return "MCP server rejected the configured credential"
        if isinstance(error, McpEndpointNotFoundError):
            return "MCP endpoint returned Not Found"
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

    @staticmethod
    def _discovery_error_code(error: BaseException) -> str:
        if isinstance(error, McpAuthenticationError):
            return "MCP_AUTHENTICATION_REJECTED"
        if isinstance(error, McpEndpointNotFoundError):
            return "MCP_ENDPOINT_NOT_FOUND"
        if isinstance(error, McpEgressPolicyError):
            return "MCP_EGRESS_BLOCKED"
        if isinstance(error, httpx.TimeoutException):
            return "MCP_DISCOVERY_TIMEOUT"
        if isinstance(error, McpResponseTooLargeError):
            return "MCP_DISCOVERY_RESPONSE_TOO_LARGE"
        if isinstance(error, httpx.RequestError):
            return "MCP_ENDPOINT_UNAVAILABLE"
        if isinstance(error, httpx.HTTPStatusError):
            return (
                "MCP_ENDPOINT_UNAVAILABLE"
                if error.response.status_code >= 500
                else "MCP_TOOL_DISCOVERY_FAILED"
            )
        if isinstance(error, (McpError, McpProtocolError, McpResponseEncodingError)):
            return "MCP_PROTOCOL_ERROR"
        return "MCP_TOOL_DISCOVERY_FAILED"


mcp_transport = McpHttpTransport()
