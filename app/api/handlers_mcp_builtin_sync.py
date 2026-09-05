from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.mcp_admin_helpers import (
    _build_server_response,
    _resolve_tools_for_server,
)
from app.api.mcp_admin_schemas import McpBuiltinServerSyncRequest, McpServerResponse
from app.api.mcp_admin_validation import registry_destination, registry_scope_options
from app.api.mcp_connection_cleanup import cleanup_server_connections
from app.api.mcp_lifecycle_guard import (
    guarded_destination_operation,
    guarded_server_operation,
)
from app.auth.service_token import require_admin_service_token
from app.config.settings import settings
from app.mcp.lifecycle import McpDestination
from app.mcp.registry.store import mcp_server_registry, tool_registry

router = APIRouter()


@router.put(
    "/servers/builtin",
    response_model=McpServerResponse,
    response_model_exclude_none=True,
)
async def sync_builtin_mcp_server(
    request: McpBuiltinServerSyncRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    """Create or replace one platform-owned built-in definition for a destination."""

    destination_id, destination_target_type = registry_destination(
        request.scope_type, request.target_id, request.target_type, request.agent_id
    )
    registry_scope = registry_scope_options(request.scope_type, destination_target_type)
    destination = McpDestination(
        workspace_id=request.workspace_id,
        scope_type=request.scope_type,
        destination_id=destination_id,
        target_type=destination_target_type,
    )
    async with guarded_destination_operation(destination):
        servers = await mcp_server_registry.list_servers(
            request.workspace_id, destination_id, **registry_scope
        )
        builtin_servers = [
            candidate
            for candidate in servers
            if getattr(candidate, "provenance_type", "manual") == "builtin"
        ]
        if len(builtin_servers) > 1:
            raise HTTPException(
                status_code=409, detail="MCP_DUPLICATE_BUILTIN_SERVER_ANOMALY"
            )
        server = builtin_servers[0] if builtin_servers else None
        if request.server_id is not None and (
            server is None or str(server.id) != request.server_id
        ):
            raise HTTPException(
                status_code=409, detail="Built-in MCP server identity changed"
            )

    async def reconcile(locked_server=None) -> McpServerResponse:
        current = locked_server or server
        if current is not None:
            refreshed = await mcp_server_registry.get_server(
                request.workspace_id,
                destination_id,
                str(current.id),
                **registry_scope,
            )
            if refreshed is None:
                raise HTTPException(status_code=409, detail="Built-in MCP server identity changed")
            current = refreshed
        trust_was_corrupt = bool(
            current
            and any(
                (
                    current.server_url != settings.BUILTIN_TARGET_MCP_SERVER_URL,
                    current.auth_type != "none",
                    current.credential_mode != "none",
                    current.auth_header_name is not None,
                    current.auth_header_prefix is not None,
                    bool(current.public_headers),
                )
            )
        )
        if trust_was_corrupt and current is not None:
            if not getattr(current, "credential_transitioning", False):
                transitioned = await mcp_server_registry.update_server(
                    request.workspace_id,
                    destination_id,
                    str(current.id),
                    {
                        "credential_transitioning": True,
                        "credential_epoch": int(
                            getattr(current, "credential_epoch", 1) or 1
                        )
                        + 1,
                        "connection_status": "error",
                        "last_discovery_error": (
                            "Built-in trust reconciliation is in progress."
                        ),
                    },
                    **registry_scope,
                )
                if transitioned is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Built-in MCP server identity changed",
                    )
                current = transitioned
            try:
                await cleanup_server_connections(
                    request.workspace_id,
                    str(current.id),
                    reason="builtin_reconciliation",
                )
            except Exception as exc:
                # The committed transition flag is intentionally retained: a
                # corrupt built-in endpoint must remain undispatchable until a
                # retry drains its credential state and atomically restores the
                # canonical secret-free definition.
                raise HTTPException(
                    status_code=503,
                    detail="Built-in MCP credential cleanup did not complete; retry sync",
                ) from exc
        existing_names = {
            tool.tool_name
            for tool in (
                await _resolve_tools_for_server(
                    request.workspace_id,
                    destination_id,
                    server_id=str(current.id),
                    **registry_scope,
                )
                if current is not None
                else []
            )
        }
        try:
            synced = await mcp_server_registry.sync_builtin_server(
                workspace_id=request.workspace_id,
                destination_id=destination_id,
                server_id=request.server_id,
                server_name=request.server_name,
                server_url=settings.BUILTIN_TARGET_MCP_SERVER_URL,
                enabled=request.enabled,
                tools=[tool.model_dump() for tool in request.tools],
                # Corrupt existing definitions bump the epoch in the durable
                # transition write before external cleanup. The final atomic
                # sync clears credential_transitioning without a second bump.
                increment_credential_epoch=False,
                **registry_scope,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="Built-in MCP server name or URL conflicts with an existing server",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        requested_names = {tool.name for tool in request.tools}
        await tool_registry.invalidate_scope_tools(
            request.workspace_id,
            request.scope_type,
            destination_id,
            existing_names | requested_names,
            target_type=destination_target_type,
        )
        server_tools = await _resolve_tools_for_server(
            request.workspace_id,
            destination_id,
            server_id=str(synced.id),
            **registry_scope,
        )
        return _build_server_response(synced, server_tools)

    if server is None:
        # Close a concurrent first-sync race under destination authority. New
        # built-in creation and its atomic tool write are DB-only; existing
        # reconciliation hands off to the narrower server guard below.
        async with guarded_destination_operation(destination):
            concurrent = [
                candidate
                for candidate in await mcp_server_registry.list_servers(
                    request.workspace_id, destination_id, **registry_scope
                )
                if getattr(candidate, "provenance_type", "manual") == "builtin"
            ]
            if len(concurrent) > 1:
                raise HTTPException(
                    status_code=409, detail="MCP_DUPLICATE_BUILTIN_SERVER_ANOMALY"
                )
            if not concurrent:
                return await reconcile(None)
            server = concurrent[0]
    async with guarded_server_operation(
        request.workspace_id,
        str(server.id),
        allow_transitioning=True,
    ) as locked_server:
        return await reconcile(locked_server)
