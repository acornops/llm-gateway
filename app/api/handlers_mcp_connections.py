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
)
from app.api.mcp_admin_schemas import (
    McpConnectionResponse,
    McpConnectionUpsertRequest,
    McpConnectionVerifyRequest,
    McpReadinessFailure,
    McpReadinessRequest,
    McpReadinessResponse,
)
from app.auth.service_token import require_admin_service_token
from app.config.settings import settings
from app.mcp.connections import (
    ConnectionOwner,
    ConnectionOwnerError,
    credential_secret_name,
    mcp_connection_store,
    resolve_connection_owner,
)
from app.mcp.header_policy import build_mcp_request_headers
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
from app.secrets.store import secret_store

router = APIRouter()
logger = structlog.get_logger()

_mutation_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
_mutation_locks_guard = asyncio.Lock()
_local_rate_windows: dict[tuple[str, str, str, str], tuple[float, int]] = {}
_local_rate_guard = asyncio.Lock()


def _connection_response(server, connection=None) -> McpConnectionResponse:
    status = connection.status if connection is not None else "missing"
    return McpConnectionResponse(
        server_id=str(server.id),
        credential_mode=server.credential_mode,
        status=status,
        auth_type=server.auth_type,
        action=(
            "connect_mcp_server"
            if status == "missing"
            else "verify_mcp_server"
            if status == "error"
            else None
        ),
        error_code=connection.error_code if connection is not None else None,
        verified_at=getattr(connection, "verified_at", None),
        updated_at=getattr(connection, "updated_at", None),
    )


async def _get_connection_server(workspace_id: str, server_id: str):
    server = await mcp_server_registry.get_server_for_workspace(workspace_id, server_id)
    if server is not None and getattr(server, "credential_transitioning", False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CREDENTIAL_TRANSITIONING",
                "message": "Credential ownership is being updated. Retry later.",
            },
        )
    if (
        server is None
        or server.credential_mode not in ("workspace", "individual")
        or server.auth_type not in ("bearer_token", "custom_header")
    ):
        raise HTTPException(status_code=404, detail="Authenticated MCP server not found")
    return server


def _request_owner(server, owner_type: str, owner_id: str) -> ConnectionOwner:
    if owner_type not in ("installation", "user"):
        raise HTTPException(status_code=422, detail="Unsupported connection owner type")
    supplied = ConnectionOwner(owner_type, owner_id)  # type: ignore[arg-type]
    try:
        resolved = resolve_connection_owner(
            server,
            "user" if owner_type == "user" else "service_identity",
            owner_id if owner_type == "user" else None,
        )
    except ConnectionOwnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if resolved != supplied:
        raise HTTPException(
            status_code=409,
            detail="Connection owner does not match credential mode",
        )
    return supplied


@asynccontextmanager
async def _mutation_lock(
    workspace_id: str, server_id: str, owner: ConnectionOwner
) -> AsyncIterator[None]:
    key = (workspace_id, server_id, owner.owner_type, owner.owner_id)
    async with _mutation_locks_guard:
        lock = _mutation_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            runtime_env = (settings.NODE_ENV or settings.APP_ENV).strip().lower()
            if runtime_env == "production":
                async with mcp_connection_store.mutation_lock(workspace_id, server_id, owner):
                    yield
            else:
                yield
    finally:
        async with _mutation_locks_guard:
            if _mutation_locks.get(key) is lock and not lock.locked():
                _mutation_locks.pop(key, None)


async def _check_mutation_rate_limit(
    workspace_id: str, server_id: str, owner: ConnectionOwner
) -> None:
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    limit = settings.MCP_CONNECTION_RATE_LIMIT_PER_WINDOW
    key_text = (
        f"mcp-connection:mutation:{workspace_id}:{server_id}:{owner.owner_type}:{owner.owner_id}"
    )
    if rate_limiter is not None:
        try:
            await rate_limiter.check_rate_limit(key_text, limit=limit, window=window)
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
        return

    key = (workspace_id, server_id, owner.owner_type, owner.owner_id)
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


async def _merge_connection_discovery(server, tools) -> list[str]:
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


async def _verify_connection(*, server, connection, workspace_id: str, credential: str):
    require_remote_mcp_enabled()
    try:
        headers = build_mcp_request_headers(
            server,
            credential,
            platform_headers={
                "x-workspace-id": workspace_id,
                "x-target-id": server.target_id,
                "x-target-type": server.target_type,
            },
        )
        tools, discovery_error = await _discover_server_tools(
            workspace_id,
            server.target_id,
            server,
            request_headers=headers,
        )
        if discovery_error is not None:
            raise ValueError("MCP discovery returned an error")
        verified_tool_names = await _merge_connection_discovery(server, tools)
        return await mcp_connection_store.set_state(
            connection,
            "connected",
            verified_tool_names=verified_tool_names,
        )
    except Exception:
        logger.warning(
            "mcp_credential_verification_failed",
            workspace_id=workspace_id,
            server_id=str(server.id),
            credential_mode=server.credential_mode,
            error_code="MCP_CREDENTIAL_VERIFICATION_FAILED",
        )
        return await mcp_connection_store.set_state(
            connection,
            "error",
            error_code="MCP_CREDENTIAL_VERIFICATION_FAILED",
        )


async def _delete_connection_secret(connection, *, reason: str) -> None:
    owner = ConnectionOwner(connection.owner_type, connection.owner_id)
    secret_name = credential_secret_name(connection.workspace_id, str(connection.server_id), owner)
    try:
        await secret_store.delete_secret(secret_name, {"workspace_id": connection.workspace_id})
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="success").inc()
    except SecretNotFoundError:
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="success").inc()
    except Exception:
        GATEWAY_MCP_SECRET_CLEANUP_TOTAL.labels(reason=reason, outcome="error").inc()
        logger.exception(
            "mcp_credential_cleanup_failed",
            workspace_id=connection.workspace_id,
            server_id=str(connection.server_id),
            owner_type=connection.owner_type,
            reason=reason,
        )
        raise


async def cleanup_server_connections(
    workspace_id: str, server_id: str, *, reason: str = "installation_delete"
) -> int:
    connections = await mcp_connection_store.list_for_server(workspace_id, server_id)
    for connection in connections:
        owner = ConnectionOwner(connection.owner_type, connection.owner_id)
        async with _mutation_lock(workspace_id, server_id, owner):
            current = await mcp_connection_store.get(workspace_id, server_id, owner)
            if current is None:
                continue
            await _delete_connection_secret(current, reason=reason)
            await mcp_connection_store.delete(workspace_id, server_id, owner)
    return len(connections)


@router.get(
    "/servers/{server_id}/connections/{owner_id}",
    response_model=McpConnectionResponse,
)
async def get_mcp_connection(
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    owner_type: str = Query(...),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpConnectionResponse:
    server = await _get_connection_server(workspace_id, server_id)
    owner = _request_owner(server, owner_type, owner_id)
    connection = await mcp_connection_store.get(workspace_id, server_id, owner)
    return _connection_response(server, connection)


@router.put(
    "/servers/{server_id}/connections/{owner_id}",
    response_model=McpConnectionResponse,
)
async def put_mcp_connection(
    request: McpConnectionUpsertRequest,
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpConnectionResponse:
    started = time.monotonic()
    outcome = "error"
    try:
        if request.owner_id != owner_id:
            raise HTTPException(status_code=422, detail="Connection owner does not match route")
        require_remote_mcp_enabled()
        server = await _get_connection_server(request.workspace_id, server_id)
        owner = _request_owner(server, request.owner_type, owner_id)
        await _check_mutation_rate_limit(request.workspace_id, server_id, owner)
        async with _mutation_lock(request.workspace_id, server_id, owner):
            existing = await mcp_connection_store.get(request.workspace_id, server_id, owner)
            previous_state = (
                (
                    existing.status,
                    list(existing.verified_tool_names or []),
                    existing.error_code,
                )
                if existing is not None
                else None
            )
            secret_name = credential_secret_name(request.workspace_id, server_id, owner)
            secret_scope = {"workspace_id": request.workspace_id}
            old_credential: str | None = None
            with suppress(SecretNotFoundError):
                old_credential = await secret_store.get_secret(secret_name, secret_scope)
            await secret_store.put_secret(secret_name, request.credential, secret_scope)
            try:
                connection = await mcp_connection_store.upsert(
                    workspace_id=request.workspace_id,
                    server_id=server_id,
                    owner=owner,
                    status="error",
                    error_code="MCP_CREDENTIAL_VERIFICATION_PENDING",
                )
                if connection is None:
                    raise HTTPException(
                        status_code=404, detail="Authenticated MCP server not found"
                    )
                verified = await _verify_connection(
                    server=server,
                    connection=connection,
                    workspace_id=request.workspace_id,
                    credential=request.credential,
                )
            except Exception:
                if old_credential is None:
                    with suppress(SecretNotFoundError):
                        await secret_store.delete_secret(secret_name, secret_scope)
                else:
                    await secret_store.put_secret(secret_name, old_credential, secret_scope)
                if existing is None:
                    await mcp_connection_store.delete(request.workspace_id, server_id, owner)
                elif previous_state is not None:
                    await mcp_connection_store.set_state(
                        existing,
                        previous_state[0],
                        verified_tool_names=previous_state[1],
                        error_code=previous_state[2],
                    )
                raise
        outcome = (
            "connected" if verified and verified.status == "connected" else "verification_error"
        )
        return _connection_response(server, verified or connection)
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(operation="connect", outcome=outcome).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="connect").observe(
            (time.monotonic() - started) * 1000
        )


@router.post(
    "/servers/{server_id}/connections/{owner_id}/verify",
    response_model=McpConnectionResponse,
)
async def verify_mcp_connection(
    request: McpConnectionVerifyRequest,
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpConnectionResponse:
    started = time.monotonic()
    outcome = "error"
    try:
        if request.owner_id != owner_id:
            raise HTTPException(status_code=422, detail="Connection owner does not match route")
        require_remote_mcp_enabled()
        server = await _get_connection_server(request.workspace_id, server_id)
        owner = _request_owner(server, request.owner_type, owner_id)
        await _check_mutation_rate_limit(request.workspace_id, server_id, owner)
        async with _mutation_lock(request.workspace_id, server_id, owner):
            connection = await mcp_connection_store.get(request.workspace_id, server_id, owner)
            if connection is None:
                raise HTTPException(status_code=404, detail="MCP connection not found")
            secret_name = credential_secret_name(request.workspace_id, server_id, owner)
            try:
                credential = await secret_store.get_secret(
                    secret_name, {"workspace_id": request.workspace_id}
                )
            except SecretNotFoundError:
                connection = (
                    await mcp_connection_store.set_state(
                        connection,
                        "error",
                        error_code="MCP_CREDENTIAL_SECRET_MISSING",
                    )
                    or connection
                )
                outcome = "secret_missing"
                return _connection_response(server, connection)
            verified = await _verify_connection(
                server=server,
                connection=connection,
                workspace_id=request.workspace_id,
                credential=credential,
            )
        outcome = (
            "connected" if verified and verified.status == "connected" else "verification_error"
        )
        return _connection_response(server, verified or connection)
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(operation="verify", outcome=outcome).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="verify").observe(
            (time.monotonic() - started) * 1000
        )


@router.delete("/servers/{server_id}/connections/{owner_id}", status_code=204)
async def delete_mcp_connection(
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    owner_type: str = Query(...),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    started = time.monotonic()
    outcome = "missing"
    try:
        server = await _get_connection_server(workspace_id, server_id)
        owner = _request_owner(server, owner_type, owner_id)
        async with _mutation_lock(workspace_id, server_id, owner):
            connection = await mcp_connection_store.get(workspace_id, server_id, owner)
            if connection is None:
                return
            await _delete_connection_secret(connection, reason="disconnect")
            await mcp_connection_store.delete(workspace_id, server_id, owner)
            outcome = "success"
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(
            operation="disconnect", outcome=outcome
        ).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="disconnect").observe(
            (time.monotonic() - started) * 1000
        )


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
        if (
            server is None
            or not server.enabled
            or getattr(server, "credential_transitioning", False)
        ):
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
            elif server.credential_mode != "none":
                try:
                    owner = resolve_connection_owner(
                        server, request.principal.type, request.principal.id
                    )
                except ConnectionOwnerError:
                    code = "MCP_INDIVIDUAL_USER_PRINCIPAL_REQUIRED"
                    owner = None
                if code is None and owner is not None:
                    connection = await mcp_connection_store.get(
                        request.workspace_id, ref.server_id, owner
                    )
                    if connection is None:
                        code = "MCP_CONNECTION_MISSING"
                        action = "connect_mcp_server"
                    elif connection.status != "connected":
                        code = "MCP_CONNECTION_ERROR"
                        action = "verify_mcp_server"
                    elif not mcp_connection_store.has_verified_tool(connection, ref.tool_name):
                        code = "MCP_CREDENTIAL_TOOL_UNAVAILABLE"
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
        await mcp_connection_store.list_for_user(workspace_id, user_id)
        if user_id is not None
        else await mcp_connection_store.list_for_workspace(workspace_id)
    )
    reason = "member_removal" if user_id is not None else "workspace_delete"
    for connection in connections:
        server_id = str(connection.server_id)
        owner = ConnectionOwner(connection.owner_type, connection.owner_id)
        async with _mutation_lock(workspace_id, server_id, owner):
            current = await mcp_connection_store.get(workspace_id, server_id, owner)
            if current is None:
                continue
            await _delete_connection_secret(current, reason=reason)
            await mcp_connection_store.delete(workspace_id, server_id, owner)
