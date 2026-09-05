"""Service-token-only terminal MCP lifecycle teardown routes."""

from __future__ import annotations

from contextlib import suppress
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.mcp_admin_helpers import _resolve_tools_for_server
from app.api.mcp_admin_schemas import McpUserLifecycleRequest
from app.api.mcp_admin_validation import (
    registry_destination,
    registry_scope_options,
)
from app.api.mcp_connection_cleanup import (
    cleanup_server_connections,
    cleanup_user_server_connection,
)
from app.api.mcp_lifecycle_guard import lifecycle_fenced_http_error
from app.auth.service_token import require_admin_service_token
from app.catalog.store import catalog_store
from app.mcp.connections import mcp_connection_store
from app.mcp.lifecycle import (
    McpDestination,
    McpLifecycleFencedError,
    McpUserLifecycleConflictError,
    McpUserLifecycleStaleError,
    mcp_lifecycle_store,
)
from app.mcp.oauth.flow_store import oauth_flow_store
from app.mcp.registry.models import McpServer
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.observability.metrics import (
    GATEWAY_MCP_USER_LIFECYCLE_CONNECTIONS_DRAINED_TOTAL,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store

router = APIRouter()
logger = structlog.get_logger()


def _teardown_failed_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "MCP_LIFECYCLE_TEARDOWN_FAILED",
            "message": "MCP lifecycle teardown did not complete; retry the request.",
            "retryable": True,
        },
    )


def _user_lifecycle_error(code: str, message: str, *, retryable: bool) -> HTTPException:
    return HTTPException(
        status_code=503 if retryable else 409,
        detail={"code": code, "message": message, "retryable": retryable},
    )


async def _teardown_server(server: McpServer) -> None:
    destination = McpDestination.from_server(server)
    registry_scope = registry_scope_options(destination.scope_type, destination.target_type)
    server_id = str(server.id)
    async with mcp_lifecycle_store.server_mutation_lock(server.workspace_id, server_id):
        await cleanup_server_connections(
            server.workspace_id,
            server_id,
            reason="lifecycle_teardown",
        )
        tools = await _resolve_tools_for_server(
            server.workspace_id,
            destination.destination_id,
            server_id=server_id,
            **registry_scope,
        )
        for tool in tools:
            await tool_registry.remove_tool(
                tool.tool_name,
                server.workspace_id,
                destination.destination_id,
                server_id=server_id,
                **registry_scope,
            )
        await mcp_server_registry.delete_server(
            server.workspace_id,
            destination.destination_id,
            server_id,
            **registry_scope,
        )


@router.delete("/destinations", status_code=204)
async def teardown_mcp_destination(
    workspace_id: str = Query(..., min_length=1),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None, min_length=1),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    destination = McpDestination(
        workspace_id=workspace_id,
        scope_type=scope_type,
        destination_id=destination_id,
        target_type=destination_target_type,
    )
    try:
        async with (
            mcp_lifecycle_store.workspace_mutation_lock(workspace_id),
            mcp_lifecycle_store.destination_mutation_lock(destination),
        ):
            await mcp_lifecycle_store.activate_destination_fence(destination)
            servers = await mcp_server_registry.list_servers(
                workspace_id,
                destination_id,
                **registry_scope_options(scope_type, destination_target_type),
            )
        # The committed terminal fence blocks new destination work. Release the
        # broad locks before external secret/OAuth cleanup; each server lock
        # drains operations that handed off before the fence was installed.
        for server in servers:
            await _teardown_server(server)
        if await mcp_server_registry.list_servers(
            workspace_id,
            destination_id,
            **registry_scope_options(scope_type, destination_target_type),
        ):
            raise RuntimeError("MCP destination teardown left registered servers")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "mcp_destination_teardown_failed",
            workspace_id=workspace_id,
            scope_type=scope_type,
            destination_id=destination_id,
            target_type=destination_target_type,
        )
        raise _teardown_failed_error() from exc


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def teardown_mcp_workspace(
    workspace_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    try:
        async with mcp_lifecycle_store.workspace_mutation_lock(workspace_id):
            await mcp_lifecycle_store.activate_workspace_fence(workspace_id)
            servers = await mcp_server_registry.list_workspace_servers(workspace_id)
        for server in servers:
            await _teardown_server(server)
        if await mcp_server_registry.list_workspace_servers(workspace_id):
            raise RuntimeError("MCP workspace teardown left registered servers")
        await oauth_flow_store.delete_for_workspace(workspace_id)
        catalog_sources = await catalog_store.list_sources(workspace_id)
        for source, _bindings in catalog_sources:
            referenced_secret_names = {
                name
                for name in (
                    source.auth_secret_name,
                    getattr(source, "previous_auth_secret_name", None),
                )
                if name
            }
            for secret_name in referenced_secret_names:
                with suppress(SecretNotFoundError):
                    await secret_store.delete_secret(
                        secret_name,
                        {"workspace_id": workspace_id},
                    )
        # Generated names cover crash-created source/bootstrap credentials that
        # never acquired a catalog row. At terminal workspace deletion every
        # row-referenced workspace secret is removed before the cursor is lost.
        await secret_store.purge_generated_catalog_secrets(workspace_id)
        if await secret_store.count_generated_catalog_secrets(workspace_id):
            raise RuntimeError("MCP workspace teardown left catalog secret objects")
        await catalog_store.delete_workspace_sources(workspace_id)
        if await catalog_store.count_workspace_sources(workspace_id):
            raise RuntimeError("MCP workspace teardown left catalog sources")
        # Purge both MCP name families across the workspace so historical
        # rowless or noncanonical-UUID secrets cannot outlive terminal deletion.
        await secret_store.purge_mcp_secrets(workspace_id)
        if await secret_store.count_mcp_secrets(workspace_id):
            raise RuntimeError("MCP workspace teardown left MCP secret objects")
        # A final fenced sweep also detects any legacy connection row whose
        # server was already missing. Such rows should be impossible under the
        # current FK, but workspace teardown is the terminal retention boundary.
        remaining = await mcp_connection_store.list_for_workspace(workspace_id)
        if remaining:
            raise RuntimeError("MCP workspace teardown left credential connections")
        await mcp_lifecycle_store.delete_user_lifecycles_for_workspace(workspace_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("mcp_workspace_teardown_failed", workspace_id=workspace_id)
        raise _teardown_failed_error() from exc


@router.put("/users/{user_id}/lifecycle", status_code=204)
async def reconcile_mcp_user_lifecycle(
    request: McpUserLifecycleRequest,
    user_id: str = Path(..., min_length=1, max_length=256),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    try:
        async with mcp_lifecycle_store.user_lifecycle_operation(
            request.workspace_id,
            user_id,
        ):
            # Keep the broad lock only for the durable stage and stable server
            # snapshot. Removed/activating state then rejects new individual
            # operations while unrelated workspace traffic remains available.
            async with mcp_lifecycle_store.workspace_mutation_lock(
                request.workspace_id
            ):
                await mcp_lifecycle_store.assert_workspace_not_fenced(
                    request.workspace_id
                )
                result = await mcp_lifecycle_store.reconcile_user_lifecycle(
                    request.workspace_id,
                    user_id,
                    request.membership_generation,
                    request.status,
                )
                if result == "idempotent" and request.status == "active":
                    return
                servers = await mcp_server_registry.list_workspace_servers(
                    request.workspace_id
                )

            # Each server lock drains a mutation authorized before the durable
            # stage. Servers created after the snapshot cannot gain this user's
            # credentials while the owner row is removed/activating.
            drained_connection_count = 0
            for server in servers:
                server_id = str(server.id)
                async with mcp_lifecycle_store.server_mutation_lock(
                    request.workspace_id,
                    server_id,
                ):
                    removed_connection = await cleanup_user_server_connection(
                        request.workspace_id,
                        server_id,
                        user_id,
                        reason=(
                            "member_reactivation"
                            if request.status == "active"
                            else "member_removal"
                        ),
                    )
                    if removed_connection:
                        drained_connection_count += 1
                        GATEWAY_MCP_USER_LIFECYCLE_CONNECTIONS_DRAINED_TOTAL.labels(
                            reason=(
                                "activation"
                                if request.status == "active"
                                else "removal"
                            )
                        ).inc()
            # One workspace-level inventory catches historical UUID aliases and
            # already-deleted server IDs without an O(members × servers) Vault
            # LIST amplification. Staged owner state remains fenced on failure.
            await secret_store.purge_mcp_secrets(
                request.workspace_id,
                owner_type="user",
                owner_id=user_id,
            )
            if await secret_store.count_mcp_secrets(
                request.workspace_id,
                owner_type="user",
                owner_id=user_id,
            ):
                raise RuntimeError("MCP user lifecycle cleanup left secret objects")
            await oauth_flow_store.delete_for_user(request.workspace_id, user_id)
            if await mcp_connection_store.list_for_user(request.workspace_id, user_id):
                raise RuntimeError("MCP user lifecycle cleanup left credential connections")
            if request.status == "active":
                # Reacquiring workspace authority makes terminal teardown win
                # before the activation CAS can expose the new generation.
                async with mcp_lifecycle_store.workspace_mutation_lock(
                    request.workspace_id
                ):
                    await mcp_lifecycle_store.assert_workspace_not_fenced(
                        request.workspace_id
                    )
                    await mcp_lifecycle_store.complete_user_activation(
                        request.workspace_id,
                        user_id,
                        request.membership_generation,
                    )
                log_activation = (
                    logger.warning if drained_connection_count else logger.info
                )
                log_activation(
                    "mcp_user_lifecycle_activation_drained_credentials",
                    workspace_id=request.workspace_id,
                    user_id=user_id,
                    membership_generation=request.membership_generation,
                    drained_connection_count=drained_connection_count,
                )
    except McpLifecycleFencedError as exc:
        raise lifecycle_fenced_http_error() from exc
    except McpUserLifecycleStaleError as exc:
        raise _user_lifecycle_error(
            "MCP_USER_LIFECYCLE_STALE",
            "The workspace membership generation is older than the gateway lifecycle state.",
            retryable=False,
        ) from exc
    except McpUserLifecycleConflictError as exc:
        raise _user_lifecycle_error(
            "MCP_USER_LIFECYCLE_CONFLICT",
            "The workspace membership generation already has a different lifecycle state.",
            retryable=False,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "mcp_user_lifecycle_teardown_failed",
            workspace_id=request.workspace_id,
            user_id=user_id,
            membership_generation=request.membership_generation,
            status=request.status,
        )
        raise _user_lifecycle_error(
            "MCP_USER_LIFECYCLE_TEARDOWN_FAILED",
            "MCP user lifecycle teardown did not complete.",
            retryable=True,
        ) from exc
