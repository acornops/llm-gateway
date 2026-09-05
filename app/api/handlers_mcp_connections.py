from __future__ import annotations

import asyncio
import time
from contextlib import suppress

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.mcp_admin_helpers import (
    _discover_server_tools,
    merge_connection_discovery,
)
from app.api.mcp_admin_schemas import (
    McpConnectionResponse,
    McpConnectionUpsertRequest,
    McpConnectionVerifyRequest,
    McpReadinessFailure,
    McpReadinessRequest,
    McpReadinessResponse,
)
from app.api.mcp_admin_validation import (
    registered_server_destination,
    registered_server_request_context,
)
from app.api.mcp_connection_cleanup import cleanup_connection_state
from app.api.mcp_connection_responses import connection_response
from app.api.mcp_lifecycle_guard import (
    assert_guarded_user_active,
    guarded_server_mutation,
    guarded_server_operation,
    user_lifecycle_stale_http_error,
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
from app.mcp.identity import canonical_mcp_server_id
from app.mcp.oauth.errors import McpOAuthError
from app.mcp.oauth.tokens import oauth_token_service
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.observability.metrics import (
    GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS,
    GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL,
    GATEWAY_MCP_READINESS_FAILURES_TOTAL,
)
from app.resilience.rate_limit import rate_limiter
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store

router = APIRouter()
logger = structlog.get_logger()

_local_rate_windows: dict[tuple[str, str, str, str], tuple[float, int]] = {}
_local_rate_guard = asyncio.Lock()


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
        or server.auth_type not in ("bearer_token", "custom_header", "oauth")
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


async def _assert_owner_generation(
    workspace_id: str,
    owner: ConnectionOwner,
    membership_generation: int | None,
) -> None:
    if owner.owner_type == "installation":
        if membership_generation is not None:
            raise HTTPException(
                status_code=422,
                detail="Installation-owned connections do not accept membership_generation",
            )
        return
    await assert_guarded_user_active(
        workspace_id,
        owner.owner_id,
        membership_generation,
    )


def _assert_connection_generation(
    connection,
    owner: ConnectionOwner,
    membership_generation: int | None,
) -> None:
    if (
        connection is not None
        and owner.owner_type == "user"
        and getattr(connection, "membership_generation", None) != membership_generation
    ):
        raise user_lifecycle_stale_http_error()


async def _check_mutation_rate_limit(
    workspace_id: str, server_id: str, owner: ConnectionOwner
) -> None:
    server_id = canonical_mcp_server_id(server_id)
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


async def _verify_connection(*, server, connection, workspace_id: str, credential: str):
    require_remote_mcp_enabled()
    destination_id, _registry_scope, platform_headers = registered_server_request_context(
        workspace_id, server
    )
    try:
        headers = build_mcp_request_headers(
            server,
            credential,
            platform_headers=platform_headers,
        )
        tools, discovery_error, discovery_error_code = await _discover_server_tools(
            workspace_id,
            destination_id,
            server,
            request_headers=headers,
        )
        if discovery_error is not None:
            error_code = discovery_error_code or "MCP_TOOL_DISCOVERY_FAILED"
            status = (
                "reauthorization_required"
                if server.auth_type == "oauth" and error_code == "MCP_AUTHENTICATION_REJECTED"
                else "error"
            )
            logger.warning(
                "mcp_connection_verification_failed",
                workspace_id=workspace_id,
                server_id=str(server.id),
                credential_mode=server.credential_mode,
                error_code=error_code,
            )
            return await mcp_connection_store.set_state(
                connection,
                status,
                error_code=error_code,
            )
        verified_tool_names = await merge_connection_discovery(server, tools)
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


@router.get(
    "/servers/{server_id}/connections/{owner_id}",
    response_model=McpConnectionResponse,
)
@guarded_server_mutation()
async def get_mcp_connection(
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    owner_type: str = Query(...),
    membership_generation: int | None = Query(default=None, ge=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpConnectionResponse:
    server = await _get_connection_server(workspace_id, server_id)
    owner = _request_owner(server, owner_type, owner_id)
    await _assert_owner_generation(workspace_id, owner, membership_generation)
    connection = await mcp_connection_store.get(workspace_id, server_id, owner)
    _assert_connection_generation(connection, owner, membership_generation)
    return connection_response(server, connection)


@router.put(
    "/servers/{server_id}/connections/{owner_id}",
    response_model=McpConnectionResponse,
)
@guarded_server_mutation()
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
        if server.auth_type == "oauth":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MCP_OAUTH_BROWSER_AUTHORIZATION_REQUIRED",
                    "message": "Use the OAuth authorization flow for this MCP server.",
                },
            )
        owner = _request_owner(server, request.owner_type, owner_id)
        await _assert_owner_generation(
            request.workspace_id,
            owner,
            request.membership_generation,
        )
        await _check_mutation_rate_limit(request.workspace_id, server_id, owner)
        async with mcp_connection_store.mutation_lock(
            request.workspace_id,
            server_id,
            owner,
        ):
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
            try:
                # Commit the non-ready state before replacing the deterministic
                # live secret. A process crash after this boundary can leave a
                # pending connection, but runtime must never observe the prior
                # connected row paired with an unverified replacement secret.
                connection = await mcp_connection_store.upsert(
                    workspace_id=request.workspace_id,
                    server_id=server_id,
                    owner=owner,
                    status="error",
                    membership_generation=request.membership_generation,
                    error_code="MCP_CREDENTIAL_VERIFICATION_PENDING",
                )
                if connection is None:
                    raise HTTPException(
                        status_code=404, detail="Authenticated MCP server not found"
                    )
                await secret_store.put_secret(
                    secret_name,
                    request.credential,
                    secret_scope,
                )
                verified = await _verify_connection(
                    server=server,
                    connection=connection,
                    workspace_id=request.workspace_id,
                    credential=request.credential,
                )
            except BaseException:
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
        return connection_response(server, verified or connection)
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(operation="connect", outcome=outcome).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="connect").observe(
            (time.monotonic() - started) * 1000
        )


@router.post(
    "/servers/{server_id}/connections/{owner_id}/verify",
    response_model=McpConnectionResponse,
)
@guarded_server_mutation()
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
        await _assert_owner_generation(
            request.workspace_id,
            owner,
            request.membership_generation,
        )
        await _check_mutation_rate_limit(request.workspace_id, server_id, owner)
        async with mcp_connection_store.mutation_lock(
            request.workspace_id,
            server_id,
            owner,
        ):
            connection = await mcp_connection_store.get(request.workspace_id, server_id, owner)
            if connection is None:
                raise HTTPException(status_code=404, detail="MCP connection not found")
            _assert_connection_generation(
                connection,
                owner,
                request.membership_generation,
            )
            if server.auth_type == "oauth":
                try:
                    access_token = await oauth_token_service.access_token(
                        workspace_id=request.workspace_id,
                        server_id=server_id,
                        owner_id=owner.owner_id,
                        connection=connection,
                        mutation_lock_held=True,
                    )
                except McpOAuthError as exc:
                    outcome = exc.code.lower()
                    return connection_response(
                        server,
                        await mcp_connection_store.get(
                            request.workspace_id,
                            server_id,
                            owner,
                        )
                        or connection,
                    )
                verified = await _verify_connection(
                    server=server,
                    connection=connection,
                    workspace_id=request.workspace_id,
                    credential=access_token,
                )
                outcome = (
                    "connected"
                    if verified and verified.status == "connected"
                    else "verification_error"
                )
                return connection_response(server, verified or connection)
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
                return connection_response(server, connection)
            verified = await _verify_connection(
                server=server,
                connection=connection,
                workspace_id=request.workspace_id,
                credential=credential,
            )
        outcome = (
            "connected" if verified and verified.status == "connected" else "verification_error"
        )
        return connection_response(server, verified or connection)
    finally:
        GATEWAY_MCP_CONNECTION_OPERATIONS_TOTAL.labels(operation="verify", outcome=outcome).inc()
        GATEWAY_MCP_CONNECTION_OPERATION_LATENCY_MS.labels(operation="verify").observe(
            (time.monotonic() - started) * 1000
        )


@router.delete("/servers/{server_id}/connections/{owner_id}", status_code=204)
@guarded_server_mutation()
async def delete_mcp_connection(
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1),
    owner_type: str = Query(...),
    membership_generation: int | None = Query(default=None, ge=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    started = time.monotonic()
    outcome = "missing"
    try:
        server = await _get_connection_server(workspace_id, server_id)
        owner = _request_owner(server, owner_type, owner_id)
        await _assert_owner_generation(workspace_id, owner, membership_generation)
        async with mcp_connection_store.mutation_lock(workspace_id, server_id, owner):
            connection = await mcp_connection_store.get(workspace_id, server_id, owner)
            _assert_connection_generation(connection, owner, membership_generation)
            await cleanup_connection_state(
                workspace_id,
                server_id,
                owner,
                connection,
                reason="disconnect",
            )
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
        server_snapshot = await mcp_server_registry.get_server_for_workspace(
            request.workspace_id,
            ref.server_id,
        )
        server = server_snapshot
        code = None
        action = None
        if server_snapshot is None:
            code = "MCP_INSTALLATION_UNAVAILABLE"
        else:
            async with guarded_server_operation(
                request.workspace_id,
                ref.server_id,
                expected_credential_epoch=int(getattr(server_snapshot, "credential_epoch", 1) or 1),
            ):
                server = await mcp_server_registry.get_server_for_workspace(
                    request.workspace_id,
                    ref.server_id,
                )
                if (
                    server is None
                    or not server.enabled
                    or getattr(
                        server,
                        "credential_transitioning",
                        False,
                    )
                ):
                    code = "MCP_INSTALLATION_UNAVAILABLE"
                    tool = None
                else:
                    destination_id, registry_scope = registered_server_destination(server)
                    tool = await tool_registry.get_tool(
                        request.workspace_id,
                        destination_id,
                        ref.tool_name,
                        server_id=ref.server_id,
                        include_disabled=True,
                        bypass_cache=True,
                        **registry_scope,
                    )
                is_trusted_builtin = (
                    tool is not None
                    and tool.source == "builtin"
                    and getattr(server, "provenance_type", "manual") == "builtin"
                )
                if code is None and (
                    tool is None
                    or not tool.enabled
                    or (not is_trusted_builtin and tool.review_state != "approved")
                ):
                    code = "MCP_INSTALLATION_UNAVAILABLE"
                elif (
                    code is None
                    and tool is not None
                    and tool.source != "builtin"
                    and not settings.REMOTE_MCP_ENABLED
                ):
                    code = "MCP_REMOTE_DISABLED"
                elif code is None and server.credential_mode != "none":
                    try:
                        owner = resolve_connection_owner(
                            server,
                            request.principal.type,
                            request.principal.id,
                        )
                    except ConnectionOwnerError:
                        code = "MCP_INDIVIDUAL_USER_PRINCIPAL_REQUIRED"
                        owner = None
                    if code is None and owner is not None:
                        if owner.owner_type == "user":
                            await assert_guarded_user_active(
                                request.workspace_id,
                                owner.owner_id,
                                request.principal.membership_generation,
                            )
                        connection = await mcp_connection_store.get(
                            request.workspace_id,
                            ref.server_id,
                            owner,
                        )
                        _assert_connection_generation(
                            connection,
                            owner,
                            request.principal.membership_generation,
                        )
                        if connection is None:
                            code = "MCP_CONNECTION_MISSING"
                            action = (
                                "authorize_mcp_server"
                                if server.auth_type == "oauth"
                                else "connect_mcp_server"
                            )
                        elif connection.status == "reauthorization_required":
                            code = "MCP_CONNECTION_ERROR"
                            action = "reauthorize_mcp_server"
                        elif connection.status != "connected":
                            code = "MCP_CONNECTION_ERROR"
                            action = (
                                "authorize_mcp_server"
                                if server.auth_type == "oauth"
                                and connection.status == "pending_authorization"
                                else "verify_mcp_server"
                            )
                        elif not mcp_connection_store.has_verified_tool(
                            connection,
                            ref.tool_name,
                        ):
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
