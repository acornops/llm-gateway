from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.mcp_admin_helpers import (
    _apply_tools_for_server,
    _discover_server_tools,
    _resolve_tools_for_server,
    secret_store,
)
from app.api.mcp_admin_schemas import (
    McpReadinessFailure,
    McpReadinessRequest,
    McpReadinessResponse,
    McpUserConnectionResponse,
    McpUserConnectionUpsertRequest,
    McpUserConnectionVerifyRequest,
)
from app.auth.service_token import require_admin_service_token
from app.config.settings import settings
from app.mcp.connections import mcp_connection_store
from app.mcp.header_policy import validate_auth_header_value
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.observability.metrics import (
    GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS,
    GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL,
    GATEWAY_MCP_READINESS_FAILURES_TOTAL,
    GATEWAY_MCP_SECRET_CLEANUP_TOTAL,
)
from app.resilience.rate_limit import rate_limiter
from app.secrets.errors import SecretNotFoundError

router = APIRouter()
logger = structlog.get_logger()

_mutation_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_mutation_locks_guard = asyncio.Lock()
_local_rate_windows: dict[tuple[str, str, str], tuple[float, int]] = {}
_local_rate_guard = asyncio.Lock()


def _connection_response(server, connection=None) -> McpUserConnectionResponse:
    status = connection.status if connection is not None else "missing"
    return McpUserConnectionResponse(
        server_id=str(server.id),
        status=status,
        auth_type=server.auth_type,
        action=(
            "connect_mcp_server"
            if status == "missing"
            else "verify_mcp_server" if status == "error" else None
        ),
        error_code=connection.error_code if connection is not None else None,
    )


async def _get_personal_server(workspace_id: str, server_id: str):
    server = await mcp_server_registry.get_server_for_workspace(workspace_id, server_id)
    if (
        server is None
        or server.auth_scope != "personal"
        or server.auth_type not in ("bearer_token", "custom_header")
    ):
        raise HTTPException(status_code=404, detail="Personal-auth MCP server not found")
    return server


def _personal_headers(server, workspace_id: str, credential: str) -> dict[str, str]:
    headers = {
        **dict(server.public_headers or {}),
        "x-workspace-id": workspace_id,
        "x-target-id": server.target_id,
        "x-target-type": server.target_type,
    }
    header_name = server.auth_header_name or "Authorization"
    prefix = server.auth_header_prefix
    if prefix is None:
        prefix = "Bearer " if server.auth_type == "bearer_token" else ""
    header_value = f"{prefix}{credential}"
    validate_auth_header_value(header_value)
    headers[header_name] = header_value
    return headers


@asynccontextmanager
async def _mutation_lock(
    workspace_id: str, server_id: str, user_id: str
) -> AsyncIterator[None]:
    key = (workspace_id, server_id, user_id)
    async with _mutation_locks_guard:
        lock = _mutation_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            runtime_env = (settings.NODE_ENV or settings.APP_ENV).strip().lower()
            if runtime_env == "production":
                async with mcp_connection_store.mutation_lock(
                    workspace_id, server_id, user_id
                ):
                    yield
            else:
                yield
    finally:
        async with _mutation_locks_guard:
            if _mutation_locks.get(key) is lock and not lock.locked():
                _mutation_locks.pop(key, None)


async def _check_mutation_rate_limit(
    operation: str, workspace_id: str, server_id: str, user_id: str
) -> None:
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    limit = settings.MCP_CONNECTION_RATE_LIMIT_PER_WINDOW
    key = (workspace_id, server_id, user_id)
    now = time.monotonic()
    async with _local_rate_guard:
        started_at, count = _local_rate_windows.get(key, (now, 0))
        if now - started_at >= window:
            started_at, count = now, 0
        count += 1
        _local_rate_windows[key] = (started_at, count)
        retry_after = max(1, int(window - (now - started_at)))
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail={"code": "MCP_CONNECTION_RATE_LIMITED", "message": "Try again later."},
            headers={"Retry-After": str(retry_after)},
        )
    if rate_limiter is not None:
        try:
            await rate_limiter.check_rate_limit(
                f"mcp-connection:{workspace_id}:{server_id}:{user_id}",
                limit=limit,
                window=window,
            )
        except HTTPException as exc:
            if exc.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "MCP_CONNECTION_RATE_LIMITED",
                        "message": "Try again later.",
                    },
                    headers={"Retry-After": str(window)},
                ) from exc
            raise


async def _merge_personal_discovery(server, tools) -> list[str]:
    existing = await _resolve_tools_for_server(
        server.workspace_id,
        server.target_id,
        server.server_url,
        target_type=server.target_type,
        server_id=str(server.id),
    )
    existing_names = {tool.tool_name for tool in existing}
    newly_observed = [tool for tool in tools if tool.name not in existing_names]
    if newly_observed:
        await _apply_tools_for_server(
            server.workspace_id,
            server.target_id,
            server.server_url,
            newly_observed,
            target_type=server.target_type,
            server_id=str(server.id),
            remove_disabled=False,
        )
    return sorted(tool.name for tool in tools)


async def _verify_connection(
    *, server, connection, workspace_id: str, user_id: str, credential: str
):
    require_remote_mcp_enabled()
    try:
        headers = _personal_headers(server, workspace_id, credential)
        tools, discovery_error = await _discover_server_tools(
            workspace_id,
            server.target_id,
            server,
            request_headers=headers,
        )
        if discovery_error is not None:
            raise ValueError("MCP discovery returned an error")
        verified_tool_names = await _merge_personal_discovery(server, tools)
        return await mcp_connection_store.set_state(
            connection,
            "connected",
            verified_tool_names=verified_tool_names,
        )
    except Exception:
        logger.warning(
            "mcp_pat_verification_failed",
            workspace_id=workspace_id,
            server_id=str(server.id),
            user_id=user_id,
            error_code="MCP_PAT_VERIFICATION_FAILED",
        )
        return await mcp_connection_store.set_state(
            connection,
            "error",
            error_code="MCP_PAT_VERIFICATION_FAILED",
        )


async def _delete_connection_secret(connection, *, reason: str) -> None:
    try:
        await secret_store.delete_secret(
            connection.access_secret_name,
            {"workspace_id": connection.workspace_id},
        )
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="success").inc()
    except SecretNotFoundError:
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="success").inc()
    except Exception:
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="error").inc()
        logger.exception(
            "mcp_personal_secret_cleanup_failed",
            workspace_id=connection.workspace_id,
            server_id=str(connection.server_id),
            user_id=connection.user_id,
            reason=reason,
        )
        raise


async def cleanup_server_connections(workspace_id: str, server_id: str) -> int:
    connections = await mcp_connection_store.list_for_server(workspace_id, server_id)
    for connection in connections:
        async with _mutation_lock(workspace_id, server_id, connection.user_id):
            current = await mcp_connection_store.get(
                workspace_id, server_id, connection.user_id
            )
            if current is None:
                continue
            await _delete_connection_secret(current, reason="installation_delete")
            await mcp_connection_store.delete(workspace_id, server_id, current.user_id)
    return len(connections)


@router.get(
    "/servers/{server_id}/connections/{user_id}",
    response_model=McpUserConnectionResponse,
)
async def get_mcp_user_connection(
    server_id: str = Path(...),
    user_id: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpUserConnectionResponse:
    server = await _get_personal_server(workspace_id, server_id)
    connection = await mcp_connection_store.get(workspace_id, server_id, user_id)
    return _connection_response(server, connection)


@router.put(
    "/servers/{server_id}/connections/{user_id}",
    response_model=McpUserConnectionResponse,
)
async def put_mcp_user_connection(
    request: McpUserConnectionUpsertRequest,
    server_id: str = Path(...),
    user_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpUserConnectionResponse:
    started = time.monotonic()
    outcome = "error"
    try:
        if request.user_id != user_id:
            raise HTTPException(status_code=422, detail="Connection user does not match route")
        await _check_mutation_rate_limit("connect", request.workspace_id, server_id, user_id)
        require_remote_mcp_enabled()
        server = await _get_personal_server(request.workspace_id, server_id)
        async with _mutation_lock(request.workspace_id, server_id, user_id):
            existing = await mcp_connection_store.get(
                request.workspace_id, server_id, user_id
            )
            secret_name = (
                existing.access_secret_name
                if existing is not None
                else f"mcp_pat::{server_id}::{user_id}"
            )
            secret_scope = {"workspace_id": request.workspace_id}
            old_credential: str | None = None
            if existing is not None:
                with suppress(SecretNotFoundError):
                    old_credential = await secret_store.get_secret(secret_name, secret_scope)
            await secret_store.put_secret(secret_name, request.credential, secret_scope)
            try:
                connection = await mcp_connection_store.upsert(
                    workspace_id=request.workspace_id,
                    server_id=server_id,
                    user_id=user_id,
                    access_secret_name=secret_name,
                    status="error",
                    error_code="MCP_PAT_VERIFICATION_PENDING",
                )
                if connection is None:
                    raise HTTPException(
                        status_code=404, detail="Personal-auth MCP server not found"
                    )
                verified = await _verify_connection(
                    server=server,
                    connection=connection,
                    workspace_id=request.workspace_id,
                    user_id=user_id,
                    credential=request.credential,
                )
            except Exception:
                if old_credential is None:
                    await secret_store.delete_secret(secret_name, secret_scope)
                else:
                    await secret_store.put_secret(secret_name, old_credential, secret_scope)
                raise
        outcome = (
            "connected"
            if verified and verified.status == "connected"
            else "profile_drift"
            if verified and verified.error_code == "MCP_INTEGRATION_PROFILE_DRIFT"
            else "verification_error"
        )
        return _connection_response(server, verified or connection)
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(
            operation="connect", outcome=outcome
        ).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="connect").observe(
            (time.monotonic() - started) * 1000
        )


@router.post(
    "/servers/{server_id}/connections/{user_id}/verify",
    response_model=McpUserConnectionResponse,
)
async def verify_mcp_user_connection(
    request: McpUserConnectionVerifyRequest,
    server_id: str = Path(...),
    user_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpUserConnectionResponse:
    started = time.monotonic()
    outcome = "error"
    try:
        if request.user_id != user_id:
            raise HTTPException(status_code=422, detail="Connection user does not match route")
        await _check_mutation_rate_limit("verify", request.workspace_id, server_id, user_id)
        require_remote_mcp_enabled()
        server = await _get_personal_server(request.workspace_id, server_id)
        async with _mutation_lock(request.workspace_id, server_id, user_id):
            connection = await mcp_connection_store.get(
                request.workspace_id, server_id, user_id
            )
            if connection is None:
                raise HTTPException(status_code=404, detail="MCP connection not found")
            try:
                credential = await secret_store.get_secret(
                    connection.access_secret_name,
                    {"workspace_id": request.workspace_id},
                )
            except SecretNotFoundError:
                connection = (
                    await mcp_connection_store.set_state(
                        connection,
                        "error",
                        error_code="MCP_PAT_SECRET_MISSING",
                    )
                    or connection
                )
                outcome = "secret_missing"
                return _connection_response(server, connection)
            verified = await _verify_connection(
                server=server,
                connection=connection,
                workspace_id=request.workspace_id,
                user_id=user_id,
                credential=credential,
            )
        outcome = (
            "connected"
            if verified and verified.status == "connected"
            else "profile_drift"
            if verified and verified.error_code == "MCP_INTEGRATION_PROFILE_DRIFT"
            else "verification_error"
        )
        return _connection_response(server, verified or connection)
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(
            operation="verify", outcome=outcome
        ).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="verify").observe(
            (time.monotonic() - started) * 1000
        )


@router.delete("/servers/{server_id}/connections/{user_id}", status_code=204)
async def delete_mcp_user_connection(
    server_id: str = Path(...),
    user_id: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    started = time.monotonic()
    outcome = "missing"
    try:
        await _get_personal_server(workspace_id, server_id)
        async with _mutation_lock(workspace_id, server_id, user_id):
            connection = await mcp_connection_store.get(workspace_id, server_id, user_id)
            if connection is None:
                return
            await _delete_connection_secret(connection, reason="disconnect")
            await mcp_connection_store.delete(workspace_id, server_id, user_id)
            outcome = "success"
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(
            operation="disconnect", outcome=outcome
        ).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(
            operation="disconnect"
        ).observe((time.monotonic() - started) * 1000)


@router.post("/connections/readiness", response_model=McpReadinessResponse)
async def check_mcp_connection_readiness(
    request: McpReadinessRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpReadinessResponse:
    failures: list[McpReadinessFailure] = []
    seen: set[tuple[str, str]] = set()
    for ref in request.tool_refs:
        key = (ref.server_id, ref.tool_name)
        if key in seen:
            continue
        seen.add(key)
        server = await mcp_server_registry.get_server_for_workspace(
            request.workspace_id, ref.server_id
        )
        code = None
        action = None
        if server is None or not server.enabled:
            code = "MCP_INSTALLATION_UNAVAILABLE"
        else:
            tool = await tool_registry.get_tool(
                request.workspace_id,
                server.target_id,
                ref.tool_name,
                target_type=server.target_type,
                server_id=ref.server_id,
                include_disabled=True,
            )
            is_trusted_builtin = (
                tool is not None
                and tool.source == "builtin"
                and getattr(server, "provenance_type", "manual") == "builtin"
            )
            if (
                tool is None
                or not tool.enabled
                or (not is_trusted_builtin and tool.review_state != "approved")
            ):
                code = "MCP_INSTALLATION_UNAVAILABLE"
            elif tool.source != "builtin" and not settings.REMOTE_MCP_ENABLED:
                code = "MCP_REMOTE_DISABLED"
            elif server.auth_scope == "personal":
                if request.principal.type != "user":
                    code = "MCP_PAT_USER_PRINCIPAL_REQUIRED"
                else:
                    connection = await mcp_connection_store.get(
                        request.workspace_id, ref.server_id, request.principal.id
                    )
                    if connection is None:
                        code = "MCP_PERSONAL_CONNECTION_MISSING"
                        action = "connect_mcp_server"
                    elif connection.status != "connected":
                        code = "MCP_PERSONAL_CONNECTION_ERROR"
                        action = "verify_mcp_server"
                    elif not mcp_connection_store.has_verified_tool(
                        connection, ref.tool_name
                    ):
                        code = "MCP_PERSONAL_TOOL_UNAVAILABLE"
                        action = "verify_mcp_server"
        if code is not None:
            GATEWAY_MCP_READINESS_FAILURES_TOTAL.labels(
                scope_type=getattr(server, "scope_type", "target"),
                reason=code.lower(),
            ).inc()
            failures.append(
                McpReadinessFailure(
                    server_id=ref.server_id,
                    tool_name=ref.tool_name,
                    code=code,
                    action=action,
                )
            )
    return McpReadinessResponse(ready=not failures, failures=failures)


@router.delete("/connections", status_code=204)
async def cleanup_mcp_connections(
    workspace_id: str = Query(..., min_length=1),
    user_id: str | None = Query(default=None, min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    connections = (
        await mcp_connection_store.list_for_principal(workspace_id, user_id)
        if user_id is not None
        else await mcp_connection_store.list_for_workspace(workspace_id)
    )
    reason = "member_removal" if user_id is not None else "workspace_delete"
    for connection in connections:
        server_id = str(connection.server_id)
        async with _mutation_lock(workspace_id, server_id, connection.user_id):
            current = await mcp_connection_store.get(
                workspace_id, server_id, connection.user_id
            )
            if current is None:
                continue
            await _delete_connection_secret(current, reason=reason)
            await mcp_connection_store.delete(
                workspace_id, server_id, connection.user_id
            )
