from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError

from app.api.handlers_mcp_connections import (
    cleanup_server_connections,
)
from app.api.handlers_mcp_connections import (
    router as mcp_connections_router,
)
from app.api.handlers_mcp_tool_admin import router as mcp_tool_admin_router
from app.api.mcp_admin_helpers import (
    _apply_tools_for_server,
    _auth_header_name_for,
    _auth_header_prefix_for,
    _build_server_request_headers,  # noqa: F401 - retained as a public test helper
    _build_server_response,
    _build_tool_response,
    _discover_server_tools,
    _record_discovery_status,
    _resolve_tools_for_server,
)
from app.api.mcp_admin_helpers import (
    _extract_discovery_error as _extract_discovery_error,
)
from app.api.mcp_admin_helpers import (
    _normalize_discovered_tools as _normalize_discovered_tools,
)
from app.api.mcp_admin_helpers import (
    mcp_transport as mcp_transport,
)
from app.api.mcp_admin_schemas import (
    McpServerConnectionTestResponse,
    McpServerCreateRequest,
    McpServerResponse,
    McpServerUpdateRequest,
    ToolConfigRequest,
    ToolConfigResponse,
)
from app.api.mcp_admin_validation import (
    is_builtin_bridge_registration,
    validate_registry_scope,
    validate_remote_mcp_endpoint_contract,
)
from app.auth.service_token import require_admin_service_token
from app.config.settings import settings
from app.examples import EXAMPLE_MCP_SERVER_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.egress_policy import McpEgressPolicyError, validate_mcp_server_url
from app.mcp.logging import loggable_mcp_server_origin
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.target_types import TARGET_TYPE_EXAMPLES

router = APIRouter()
router.include_router(mcp_connections_router)
router.include_router(mcp_tool_admin_router)
logger = structlog.get_logger()


@router.get("/servers", response_model=list[McpServerResponse])
async def list_mcp_servers(
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> list[McpServerResponse]:
    validate_registry_scope(scope_type, target_id, target_type, agent_id)
    servers = await mcp_server_registry.list_servers(
        workspace_id, target_id, target_type=target_type
    )
    response: list[McpServerResponse] = []
    for server in servers:
        server_tools = await _resolve_tools_for_server(
            workspace_id,
            target_id,
            server.server_url,
            target_type=server.target_type,
            server_id=str(server.id),
        )
        response.append(_build_server_response(server, server_tools))
    return response


@router.get("/tools", response_model=list[ToolConfigResponse])
async def list_mcp_tools(
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    include_server_disabled: bool = Query(default=False),
    include_disabled: bool = Query(default=False),
    _token_ok: None = Depends(require_admin_service_token),
) -> list[ToolConfigResponse]:
    validate_registry_scope(scope_type, target_id, target_type, agent_id)
    tools = await tool_registry.list_target_tools(
        workspace_id,
        target_id,
        target_type=target_type,
        include_disabled=include_disabled,
    )
    response: list[ToolConfigResponse] = []
    for tool in tools:
        server = await mcp_server_registry.get_server_by_url(
            workspace_id,
            target_id,
            tool.mcp_server_url,
            enabled_only=False,
            target_type=tool.target_type,
        )
        if not include_server_disabled and server and not server.enabled:
            continue
        response.append(_build_tool_response(tool))
    return response


@router.post("/servers", response_model=McpServerResponse, status_code=201)
async def create_mcp_server(
    request: McpServerCreateRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    assert request.target_id is not None and request.target_type is not None
    is_builtin_bridge = is_builtin_bridge_registration(request)
    if any(tool.source == "builtin" for tool in request.tools) and not is_builtin_bridge:
        raise HTTPException(
            status_code=400, detail="Only the platform built-in bridge may register built-in tools"
        )
    if not is_builtin_bridge:
        validate_remote_mcp_endpoint_contract(request.server_url)
        try:
            await validate_mcp_server_url(request.server_url)
        except McpEgressPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    auth_header_name = _auth_header_name_for(request.auth_type, request.auth_header_name)
    auth_header_prefix = _auth_header_prefix_for(request.auth_type, request.auth_header_prefix)

    try:
        server = await mcp_server_registry.create_server(
            workspace_id=request.workspace_id,
            target_id=request.target_id,
            target_type=request.target_type,
            server_name=request.server_name,
            server_url=request.server_url,
            enabled=request.enabled,
            auth_type=request.auth_type,
            auth_header_name=auth_header_name,
            auth_header_prefix=auth_header_prefix,
            public_headers=request.public_headers,
            credential_mode=request.credential_mode,
            target_constraints=request.target_constraints.model_dump(),
            provenance_type="builtin" if is_builtin_bridge else "manual",
            endpoint_configuration=None,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="MCP server name or URL already exists in target",
        ) from exc

    tools_to_apply = request.tools
    discovery_error: str | None = None
    # Authenticated installations have no usable credential until the resolved
    # owner creates its connection. That connection flow owns the
    # first authenticated discovery and its connected/error state.
    if len(tools_to_apply) == 0 and request.credential_mode == "none":
        if not is_builtin_bridge:
            require_remote_mcp_enabled()
        try:
            tools_to_apply, discovery_error = await _discover_server_tools(
                request.workspace_id, request.target_id, server
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            discovery_error = detail or "MCP server discovery failed."
            logger.warning(
                "mcp_tool_discovery_validation_failed",
                workspace_id=request.workspace_id,
                target_id=request.target_id,
                server_name=request.server_name,
                server_url=loggable_mcp_server_origin(request.server_url),
                error_code="MCP_DISCOVERY_VALIDATION_FAILED",
            )
            tools_to_apply = []
        except Exception:
            logger.exception(
                "mcp_tool_discovery_failed",
                workspace_id=request.workspace_id,
                target_id=request.target_id,
                server_name=request.server_name,
                server_url=loggable_mcp_server_origin(request.server_url),
            )
            discovery_error = "MCP server discovery failed."
            tools_to_apply = []

        updated_server = await _record_discovery_status(
            request.workspace_id,
            request.target_id,
            str(server.id),
            discovery_error,
            target_type=request.target_type,
        )
        if updated_server is not None:
            server = updated_server

    await _apply_tools_for_server(
        request.workspace_id,
        request.target_id,
        server.server_url,
        tools_to_apply,
        target_type=request.target_type,
        server_id=str(server.id),
        remove_disabled=len(request.tools) > 0,
    )

    server_tools = await _resolve_tools_for_server(
        request.workspace_id,
        request.target_id,
        request.server_url,
        target_type=request.target_type,
        server_id=str(server.id),
    )
    return _build_server_response(server, server_tools)


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
async def update_mcp_server(
    request: McpServerUpdateRequest,
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    validate_registry_scope(scope_type, target_id, target_type, agent_id)
    server = await mcp_server_registry.get_server(
        workspace_id, target_id, server_id, target_type=target_type
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    next_auth_type = request.auth_type if request.auth_type is not None else server.auth_type
    if next_auth_type == "custom_header":
        if request.auth_header_name is not None:
            next_auth_header_name = request.auth_header_name
        elif server.auth_type == "custom_header":
            next_auth_header_name = server.auth_header_name
        else:
            next_auth_header_name = None
    elif next_auth_type == "bearer_token":
        next_auth_header_name = "Authorization"
    else:
        next_auth_header_name = None

    if next_auth_type == "custom_header":
        next_auth_header_prefix = (
            request.auth_header_prefix
            if request.auth_header_prefix is not None
            else server.auth_header_prefix
        )
    elif next_auth_type == "bearer_token":
        next_auth_header_prefix = "Bearer "
    else:
        next_auth_header_prefix = None

    request_has_auth_fields = any(
        value is not None
        for value in (
            request.auth_header_name,
            request.auth_header_prefix,
        )
    )
    if next_auth_type == "none" and request_has_auth_fields:
        raise HTTPException(
            status_code=400,
            detail="auth fields are not allowed when auth_type is none",
        )
    if next_auth_type == "custom_header" and not next_auth_header_name:
        raise HTTPException(
            status_code=400,
            detail="auth_header_name is required for custom_header auth",
        )
    next_credential_mode = (
        request.credential_mode if request.credential_mode is not None else server.credential_mode
    )
    if next_auth_type == "none" and next_credential_mode != "none":
        raise HTTPException(
            status_code=400,
            detail="credential_mode must be none when auth_type is none",
        )
    if next_auth_type != "none" and next_credential_mode == "none":
        raise HTTPException(
            status_code=400,
            detail="authenticated MCP installations require a credential mode",
        )
    patch: dict[str, Any] = {}
    if request.server_url is not None:
        if (
            getattr(server, "provenance_type", "manual") != "builtin"
            or request.server_url != settings.BUILTIN_TARGET_MCP_SERVER_URL
        ):
            raise HTTPException(
                status_code=400,
                detail="Only a built-in server may rotate to the configured built-in endpoint",
            )
        patch["server_url"] = request.server_url
    if request.server_name is not None:
        patch["server_name"] = request.server_name
    if request.enabled is not None:
        patch["enabled"] = request.enabled
    if request.auth_type is not None:
        patch["auth_type"] = request.auth_type
    if request.credential_mode is not None or request.auth_type == "none":
        patch["credential_mode"] = next_credential_mode
    if request.auth_type is not None:
        patch["auth_header_name"] = next_auth_header_name
        patch["auth_header_prefix"] = next_auth_header_prefix
    else:
        if next_auth_type == "bearer_token" and request_has_auth_fields:
            patch["auth_header_name"] = next_auth_header_name
            patch["auth_header_prefix"] = next_auth_header_prefix
        elif next_auth_type == "custom_header":
            if request.auth_header_name is not None:
                patch["auth_header_name"] = next_auth_header_name
            if request.auth_header_prefix is not None:
                patch["auth_header_prefix"] = next_auth_header_prefix
    if request.public_headers is not None:
        patch["public_headers"] = request.public_headers
    if request.target_constraints is not None:
        patch["target_constraints"] = request.target_constraints.model_dump()
    if request.tools is not None:
        requested_sources = {tool.source for tool in request.tools if tool.source is not None}
        if (
            "builtin" in requested_sources
            and getattr(server, "provenance_type", "manual") != "builtin"
        ):
            raise HTTPException(
                status_code=400,
                detail="Manual and catalog servers cannot claim built-in tool source",
            )
        if "mcp" in requested_sources and getattr(server, "provenance_type", "manual") == "builtin":
            raise HTTPException(
                status_code=400, detail="Built-in servers may contain built-in tools only"
            )
    if request.expected_revision is not None:
        patch["expected_revision"] = request.expected_revision

    if patch:
        trust_changed = any(
            (
                request.server_url is not None and request.server_url != server.server_url,
                next_auth_type != server.auth_type,
                next_auth_header_name != server.auth_header_name,
                next_auth_header_prefix != server.auth_header_prefix,
                next_credential_mode != server.credential_mode,
                request.public_headers is not None
                and request.public_headers != (server.public_headers or {}),
            )
        )
        if trust_changed:
            reason = (
                "mode_transition"
                if next_credential_mode != server.credential_mode
                else "trust_change"
            )
            transition_patch: dict[str, Any] = {
                "credential_transitioning": True,
                "connection_status": "error",
                "last_discovery_at": None,
                "last_discovery_error": "Credential configuration update in progress.",
            }
            if request.expected_revision is not None:
                transition_patch["expected_revision"] = request.expected_revision
            try:
                transitioning = await mcp_server_registry.update_server(
                    workspace_id,
                    target_id,
                    server_id,
                    transition_patch,
                    target_type=target_type,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not transitioning:
                raise HTTPException(status_code=404, detail="MCP server not found")
            patch.pop("expected_revision", None)
            await cleanup_server_connections(workspace_id, server_id, reason=reason)
            logger.info(
                "mcp_connections_invalidated_for_trust_change",
                workspace_id=workspace_id,
                scope_type=scope_type,
                server_id=server_id,
                reason=reason,
            )
            patch["credential_transitioning"] = False
            patch["connection_status"] = "unknown"
            patch["last_discovery_at"] = None
            patch["last_discovery_error"] = None
        try:
            updated = await mcp_server_registry.update_server(
                workspace_id,
                target_id,
                server_id,
                patch,
                target_type=target_type,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="MCP server name or URL already exists in target",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="MCP server not found")
        server = updated

    if request.tools:
        await _apply_tools_for_server(
            workspace_id,
            target_id,
            server.server_url,
            request.tools,
            target_type=server.target_type,
            server_id=str(server.id),
        )
    elif not request.remove_tools:
        # Recovery path: if a server has no tools mapped, try discovery on update.
        current_tools = await _resolve_tools_for_server(
            workspace_id,
            target_id,
            server.server_url,
            target_type=server.target_type,
            server_id=str(server.id),
        )
        if len(current_tools) == 0 and server.credential_mode == "none":
            if getattr(server, "provenance_type", "manual") != "builtin":
                require_remote_mcp_enabled()
            discovery_error: str | None = None
            try:
                discovered_tools, discovery_error = await _discover_server_tools(
                    workspace_id, target_id, server
                )
                if len(discovered_tools) > 0:
                    await _apply_tools_for_server(
                        workspace_id,
                        target_id,
                        server.server_url,
                        discovered_tools,
                        target_type=server.target_type,
                        server_id=str(server.id),
                        remove_disabled=False,
                    )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                discovery_error = detail or "MCP server discovery failed."
                logger.warning(
                    "mcp_tool_discovery_validation_failed_on_update",
                    workspace_id=workspace_id,
                    target_id=target_id,
                    server_name=server.server_name,
                    server_url=loggable_mcp_server_origin(server.server_url),
                    error_code="MCP_DISCOVERY_VALIDATION_FAILED",
                )
            except Exception:
                logger.exception(
                    "mcp_tool_discovery_failed_on_update",
                    workspace_id=workspace_id,
                    target_id=target_id,
                    server_name=server.server_name,
                    server_url=loggable_mcp_server_origin(server.server_url),
                )
                discovery_error = "MCP server discovery failed."

            updated_server = await _record_discovery_status(
                workspace_id,
                target_id,
                str(server.id),
                discovery_error,
                target_type=target_type,
            )
            if updated_server is not None:
                server = updated_server

    if request.remove_tools:
        for tool_name in request.remove_tools:
            await tool_registry.remove_tool_for_target(
                tool_name,
                workspace_id,
                target_id,
                target_type=server.target_type,
                server_id=str(server.id),
            )

    server_tools = await _resolve_tools_for_server(
        workspace_id,
        target_id,
        server.server_url,
        target_type=server.target_type,
        server_id=str(server.id),
    )
    return _build_server_response(server, server_tools)


@router.post("/servers/{server_id}/test", response_model=McpServerConnectionTestResponse)
async def test_mcp_server_connection(
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerConnectionTestResponse:
    validate_registry_scope(scope_type, target_id, target_type, agent_id)
    server = await mcp_server_registry.get_server(
        workspace_id, target_id, server_id, target_type=target_type
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    if server.credential_mode != "none":
        raise HTTPException(
            status_code=409,
            detail="Use the connection verify endpoint for authenticated discovery",
        )
    if getattr(server, "provenance_type", "manual") != "builtin":
        require_remote_mcp_enabled()

    discovered_tools: list[ToolConfigRequest] = []
    discovery_error: str | None = None
    try:
        discovered_tools, discovery_error = await _discover_server_tools(
            workspace_id, target_id, server
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        discovery_error = detail or "MCP server discovery failed."
    except Exception:
        logger.exception(
            "mcp_tool_discovery_test_failed",
            workspace_id=workspace_id,
            target_id=target_id,
            server_name=server.server_name,
            server_url=loggable_mcp_server_origin(server.server_url),
        )
        discovery_error = "MCP server discovery failed."

    updated_server = await _record_discovery_status(
        workspace_id,
        target_id,
        server_id,
        discovery_error,
        target_type=target_type,
    )
    if updated_server is not None:
        server = updated_server

    timestamp = server.last_discovery_at or datetime.now(UTC)
    discovered_tool_names = sorted({tool.name for tool in discovered_tools})
    return McpServerConnectionTestResponse(
        server_id=str(server.id),
        server_name=server.server_name,
        server_url=server.server_url,
        connection_status="error" if discovery_error else "ok",
        last_discovery_at=timestamp,
        discovered_tool_count=len(discovered_tool_names),
        discovered_tools=discovered_tool_names,
        error=discovery_error,
    )


@router.delete("/servers/{server_id}", status_code=204)
async def delete_mcp_server(
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    validate_registry_scope(scope_type, target_id, target_type, agent_id)
    server = await mcp_server_registry.get_server(
        workspace_id, target_id, server_id, target_type=target_type
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    await cleanup_server_connections(workspace_id, server_id)

    server_tools = await _resolve_tools_for_server(
        workspace_id,
        target_id,
        server.server_url,
        target_type=server.target_type,
        server_id=str(server.id),
    )
    for tool in server_tools:
        await tool_registry.remove_tool_for_target(
            tool.tool_name,
            workspace_id,
            target_id,
            target_type=server.target_type,
            server_id=str(server.id),
        )

    deleted = await mcp_server_registry.delete_server(
        workspace_id,
        target_id,
        server_id,
        target_type=target_type,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="MCP server not found")
