from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError

from app.api.handlers_mcp_builtin_sync import router as mcp_builtin_sync_router
from app.api.handlers_mcp_connections import (
    router as mcp_connections_router,
)
from app.api.handlers_mcp_lifecycle import router as mcp_lifecycle_router
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
    merge_connection_discovery,
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
    ToolConfigResponse,
)
from app.api.mcp_admin_validation import (
    registry_destination,
    registry_scope_options,
    validate_remote_mcp_endpoint_contract,
)
from app.api.mcp_connection_cleanup import cleanup_server_connections
from app.api.mcp_connection_test import run_mcp_server_connection_test
from app.api.mcp_lifecycle_guard import (
    guarded_destination_operation,
    guarded_destination_read,
    guarded_server_mutation,
    guarded_server_operation,
)
from app.api.mcp_trust_reconciliation import reconcile_credential_free_trust
from app.auth.service_token import require_admin_service_token
from app.examples import EXAMPLE_MCP_SERVER_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.egress_policy import McpEgressPolicyError, validate_mcp_server_url
from app.mcp.header_policy import validate_public_auth_header_collision
from app.mcp.lifecycle import McpDestination
from app.mcp.logging import loggable_mcp_server_origin
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.target_types import TARGET_TYPE_EXAMPLES

router = APIRouter()
router.include_router(mcp_connections_router)
router.include_router(mcp_builtin_sync_router)
router.include_router(mcp_tool_admin_router)
router.include_router(mcp_lifecycle_router)
logger = structlog.get_logger()


@router.get("/servers", response_model=list[McpServerResponse], response_model_exclude_none=True)
@guarded_destination_read()
async def list_mcp_servers(
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> list[McpServerResponse]:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    registry_scope = registry_scope_options(scope_type, destination_target_type)
    servers = await mcp_server_registry.list_servers(workspace_id, destination_id, **registry_scope)
    response: list[McpServerResponse] = []
    for server in servers:
        server_tools = await _resolve_tools_for_server(
            workspace_id,
            destination_id,
            server_id=str(server.id),
            **registry_scope,
        )
        response.append(_build_server_response(server, server_tools))
    return response


@router.get("/tools", response_model=list[ToolConfigResponse], response_model_exclude_none=True)
@guarded_destination_read()
async def list_mcp_tools(
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    include_server_disabled: bool = Query(default=False),
    include_disabled: bool = Query(default=False),
    _token_ok: None = Depends(require_admin_service_token),
) -> list[ToolConfigResponse]:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    registry_scope = registry_scope_options(scope_type, destination_target_type)
    tools = await tool_registry.list_tools(
        workspace_id,
        destination_id,
        include_disabled=include_disabled,
        **registry_scope,
    )
    response: list[ToolConfigResponse] = []
    for tool in tools:
        server = await mcp_server_registry.get_server(
            workspace_id,
            destination_id,
            str(tool.server_id),
            **registry_scope,
        )
        if server is None:
            logger.warning(
                "mcp_orphan_tool_omitted",
                workspace_id=workspace_id,
                scope_type=scope_type,
                destination_id=destination_id,
                server_id=str(tool.server_id),
                tool_name=tool.tool_name,
            )
            continue
        if not include_server_disabled and not server.enabled:
            continue
        response.append(_build_tool_response(tool))
    return response


@router.post(
    "/servers",
    response_model=McpServerResponse,
    response_model_exclude_none=True,
    status_code=201,
)
async def create_mcp_server(
    request: McpServerCreateRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    destination_id, destination_target_type = registry_destination(
        request.scope_type, request.target_id, request.target_type, request.agent_id
    )
    registry_scope = registry_scope_options(request.scope_type, destination_target_type)
    if any(tool.source == "builtin" for tool in request.tools):
        raise HTTPException(
            status_code=409,
            detail="Platform built-in definitions must use the built-in synchronization endpoint",
        )
    discover_on_create = len(request.tools) == 0 and request.credential_mode == "none"
    if discover_on_create:
        require_remote_mcp_enabled()
    validate_remote_mcp_endpoint_contract(request.server_url)
    try:
        await validate_mcp_server_url(request.server_url)
    except McpEgressPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    auth_header_name = _auth_header_name_for(request.auth_type, request.auth_header_name)
    auth_header_prefix = _auth_header_prefix_for(request.auth_type, request.auth_header_prefix)

    destination = McpDestination(
        workspace_id=request.workspace_id,
        scope_type=request.scope_type,
        destination_id=destination_id,
        target_type=destination_target_type,
    )
    async with guarded_destination_operation(destination):
        try:
            server = await mcp_server_registry.create_server(
                workspace_id=request.workspace_id,
                destination_id=destination_id,
                server_name=request.server_name,
                server_url=request.server_url,
                enabled=request.enabled,
                auth_type=request.auth_type,
                auth_header_name=auth_header_name,
                auth_header_prefix=auth_header_prefix,
                public_headers=request.public_headers,
                credential_mode=request.credential_mode,
                provenance_type="manual",
                endpoint_configuration=None,
                **registry_scope,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="MCP server name or URL already exists in target",
            ) from exc
        server_id = str(server.id)

    async with guarded_server_operation(
        request.workspace_id,
        server_id,
    ):
        # The lifecycle guard is an authority/serialization boundary, not a
        # response DTO contract. Reload the complete registry row under that
        # guard and keep using the canonical ID captured at creation.
        server = await mcp_server_registry.get_server(
            request.workspace_id,
            destination_id,
            server_id,
            **registry_scope,
        )
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        tools_to_apply = request.tools
        discovery_error: str | None = None
        # Authenticated installations have no usable credential until the resolved
        # owner creates its connection. That connection flow owns the
        # first authenticated discovery and its connected/error state.
        if discover_on_create:
            try:
                tools_to_apply, discovery_error, _discovery_error_code = (
                    await _discover_server_tools(request.workspace_id, destination_id, server)
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                discovery_error = detail or "MCP server discovery failed."
                logger.warning(
                    "mcp_tool_discovery_validation_failed",
                    workspace_id=request.workspace_id,
                    scope_type=request.scope_type,
                    destination_id=destination_id,
                    server_name=request.server_name,
                    server_url=loggable_mcp_server_origin(request.server_url),
                    error_code="MCP_DISCOVERY_VALIDATION_FAILED",
                )
                tools_to_apply = []
            except Exception:
                logger.exception(
                    "mcp_tool_discovery_failed",
                    workspace_id=request.workspace_id,
                    scope_type=request.scope_type,
                    destination_id=destination_id,
                    server_name=request.server_name,
                    server_url=loggable_mcp_server_origin(request.server_url),
                )
                discovery_error = "MCP server discovery failed."
                tools_to_apply = []

            updated_server = await _record_discovery_status(
                request.workspace_id,
                destination_id,
                server_id,
                discovery_error,
                **registry_scope,
            )
            if updated_server is not None:
                server = updated_server

        await _apply_tools_for_server(
            request.workspace_id,
            destination_id,
            tools_to_apply,
            server_id=server_id,
            remove_disabled=len(request.tools) > 0,
            **registry_scope,
        )

        server_tools = await _resolve_tools_for_server(
            request.workspace_id,
            destination_id,
            server_id=server_id,
            **registry_scope,
        )
        return _build_server_response(server, server_tools)


@router.patch(
    "/servers/{server_id}",
    response_model=McpServerResponse,
    response_model_exclude_none=True,
)
@guarded_server_mutation(allow_transitioning=True)
async def update_mcp_server(
    request: McpServerUpdateRequest,
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    registry_scope = registry_scope_options(scope_type, destination_target_type)
    server = await mcp_server_registry.get_server(
        workspace_id,
        destination_id,
        server_id,
        **registry_scope,
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    if getattr(server, "provenance_type", "manual") == "builtin":
        supplied_fields = request.model_fields_set
        if request.enabled is None or not supplied_fields.issubset(
            {"enabled", "expected_revision"}
        ):
            raise HTTPException(
                status_code=409,
                detail="Built-in MCP servers allow enablement changes only",
            )
        patch: dict[str, Any] = {"enabled": request.enabled}
        if request.expected_revision is not None:
            patch["expected_revision"] = request.expected_revision
        try:
            updated = await mcp_server_registry.update_server(
                workspace_id,
                destination_id,
                server_id,
                patch,
                **registry_scope,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        server_tools = await _resolve_tools_for_server(
            workspace_id,
            destination_id,
            server_id=str(updated.id),
            **registry_scope,
        )
        return _build_server_response(updated, server_tools)

    next_auth_type = request.auth_type if request.auth_type is not None else server.auth_type
    if next_auth_type == "custom_header":
        if request.auth_header_name is not None:
            next_auth_header_name = request.auth_header_name
        elif server.auth_type == "custom_header":
            next_auth_header_name = server.auth_header_name
        else:
            next_auth_header_name = None
    elif next_auth_type in ("bearer_token", "oauth"):
        next_auth_header_name = "Authorization"
    else:
        next_auth_header_name = None

    if next_auth_type == "custom_header":
        next_auth_header_prefix = (
            request.auth_header_prefix
            if request.auth_header_prefix is not None
            else server.auth_header_prefix
            if server.auth_type == "custom_header"
            else ""
        )
    elif next_auth_type in ("bearer_token", "oauth"):
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
    try:
        validate_public_auth_header_collision(
            request.public_headers
            if request.public_headers is not None
            else server.public_headers,
            next_auth_type,
            next_auth_header_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if next_auth_type == "oauth" and request_has_auth_fields:
        raise HTTPException(
            status_code=400,
            detail="OAuth MCP installations do not accept auth header fields",
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
    if next_auth_type == "oauth" and next_credential_mode != "individual":
        raise HTTPException(
            status_code=400,
            detail="OAuth MCP installations require individual credentials",
        )
    patch: dict[str, Any] = {}
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
        if next_auth_type in ("bearer_token", "oauth") and request_has_auth_fields:
            patch["auth_header_name"] = next_auth_header_name
            patch["auth_header_prefix"] = next_auth_header_prefix
        elif next_auth_type == "custom_header":
            if request.auth_header_name is not None:
                patch["auth_header_name"] = next_auth_header_name
            if request.auth_header_prefix is not None:
                patch["auth_header_prefix"] = next_auth_header_prefix
    if request.public_headers is not None:
        patch["public_headers"] = request.public_headers
    if request.expected_revision is not None:
        patch["expected_revision"] = request.expected_revision

    trust_changed = any(
        (
            next_auth_type != server.auth_type,
            next_auth_header_name != server.auth_header_name,
            next_auth_header_prefix != server.auth_header_prefix,
            next_credential_mode != server.credential_mode,
            request.public_headers is not None
            and request.public_headers != (server.public_headers or {}),
        )
    )
    if (
        patch
        and next_credential_mode == "none"
        and (
            trust_changed
            or bool(getattr(server, "credential_transitioning", False))
        )
    ):
        # The kill switch must win before transition persistence, credential
        # cleanup, DNS validation, or credential-free discovery side effects.
        require_remote_mcp_enabled()

    if patch:
        transition_required = trust_changed or bool(
            getattr(server, "credential_transitioning", False)
        )
        credential_free_reconciliation = False
        if transition_required:
            reason = (
                "mode_transition"
                if next_credential_mode != server.credential_mode
                else "trust_change"
                if trust_changed
                else "trust_change_recovery"
            )
            persisted_request_matches = all(
                getattr(server, key, None) == value
                for key, value in patch.items()
                if key != "expected_revision"
            )
            recovery_retry = (
                bool(getattr(server, "credential_transitioning", False))
                and not trust_changed
                and persisted_request_matches
            )
            if recovery_retry:
                # Resume the durably applied transition without replaying its stale revision.
                patch = {}
            else:
                transition_patch: dict[str, Any] = {
                    **patch,
                    "credential_transitioning": True,
                    "credential_epoch": int(getattr(server, "credential_epoch", 1) or 1) + 1,
                    "connection_status": "error",
                    "last_discovery_at": None,
                    "last_discovery_error": "Credential configuration update in progress.",
                }
                try:
                    transitioning = await mcp_server_registry.update_server(
                        workspace_id,
                        destination_id,
                        server_id,
                        transition_patch,
                        **registry_scope,
                    )
                except IntegrityError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail="MCP server name or URL already exists in target",
                    ) from exc
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if not transitioning:
                    raise HTTPException(status_code=404, detail="MCP server not found")
                server = transitioning
            try:
                await cleanup_server_connections(workspace_id, server_id, reason=reason)
            except Exception as exc:
                logger.exception(
                    "mcp_connection_cleanup_failed_for_trust_change",
                    workspace_id=workspace_id,
                    scope_type=scope_type,
                    server_id=server_id,
                    reason=reason,
                )
                raise HTTPException(
                    status_code=503,
                    detail="MCP credential cleanup did not complete; retry this update",
                ) from exc
            logger.info(
                "mcp_connections_invalidated_for_trust_change",
                workspace_id=workspace_id,
                scope_type=scope_type,
                server_id=server_id,
                reason=reason,
            )
            # Tool review authority is bound to the endpoint plus its request
            # authentication context. Reconnection/discovery must recreate
            # every definition disabled and pending review after any trust
            # transition; compatible names must not inherit old approvals.
            await tool_registry.remove_server_tools_not_in(
                workspace_id,
                destination_id,
                server_id=server_id,
                tool_names=set(),
                **registry_scope,
            )
            credential_free_reconciliation = next_credential_mode == "none"
            patch = (
                {}
                if credential_free_reconciliation
                else {
                    "credential_transitioning": False,
                    "connection_status": "unknown",
                    "last_discovery_at": None,
                    "last_discovery_error": None,
                }
            )
        if patch:
            try:
                updated = await mcp_server_registry.update_server(
                    workspace_id,
                    destination_id,
                    server_id,
                    patch,
                    **registry_scope,
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

        if credential_free_reconciliation:
            server = await reconcile_credential_free_trust(
                workspace_id=workspace_id,
                destination_id=destination_id,
                server_id=server_id,
                server=server,
                registry_scope=registry_scope,
            )

    server_tools = await _resolve_tools_for_server(
        workspace_id,
        destination_id,
        server_id=str(server.id),
        **registry_scope,
    )
    return _build_server_response(server, server_tools)


@router.post("/servers/{server_id}/test", response_model=McpServerConnectionTestResponse)
@guarded_server_mutation()
async def test_mcp_server_connection(
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerConnectionTestResponse:
    return await run_mcp_server_connection_test(
        server_id=server_id,
        workspace_id=workspace_id,
        target_id=target_id,
        target_type=target_type,
        scope_type=scope_type,
        agent_id=agent_id,
        registry_destination=registry_destination,
        registry_scope_options=registry_scope_options,
        server_registry=mcp_server_registry,
        require_remote_mcp_enabled=require_remote_mcp_enabled,
        discover_server_tools=_discover_server_tools,
        merge_connection_discovery=merge_connection_discovery,
        record_discovery_status=_record_discovery_status,
        logger=logger,
    )
