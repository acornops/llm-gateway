from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError

from app.api.mcp_admin_helpers import (
    _apply_tools_for_server,
    _auth_header_name_for,
    _auth_header_prefix_for,
    _build_server_response,
    _build_tool_response,
    _discover_server_tools,
    _persist_server_secret,
    _record_discovery_status,
    _resolve_tools_for_server,
)
from app.api.mcp_admin_helpers import (
    _build_server_request_headers as _build_server_request_headers,
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
from app.api.mcp_admin_helpers import (
    secret_store as secret_store,
)
from app.api.mcp_admin_schemas import (
    McpServerConnectionTestResponse,
    McpServerCreateRequest,
    McpServerResponse,
    McpServerUpdateRequest,
    ToolConfigRequest,
    ToolConfigResponse,
    ToolUpdateRequest,
)
from app.auth.service_token import require_admin_service_token
from app.config.settings import settings
from app.examples import EXAMPLE_MCP_SERVER_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.egress_policy import McpEgressPolicyError, validate_mcp_server_url
from app.mcp.header_policy import validate_auth_header_value
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.target_types import TARGET_TYPE_EXAMPLES

router = APIRouter()
logger = structlog.get_logger()


def _validate_registry_scope(scope_type: str, target_id: str, target_type: str) -> None:
    if scope_type == "workspace":
        if target_id != "__workspace__" or target_type != "workspace":
            raise HTTPException(
                status_code=422,
                detail="workspace scope requires target_id=__workspace__ and target_type=workspace",
            )
    elif target_type == "workspace":
        raise HTTPException(status_code=422, detail="target scope requires a concrete target_type")


def _is_builtin_bridge_registration(request: McpServerCreateRequest) -> bool:
    return (
        request.server_name == settings.BUILTIN_MCP_SERVER_NAME
        and request.server_url == settings.BUILTIN_MCP_SERVER_URL
        and request.auth_type == "none"
        and request.auth_secret_name is None
        and request.auth_secret_value is None
        and request.auth_header_name is None
        and request.auth_header_prefix is None
        and request.public_headers is None
        and len(request.tools) > 0
        and all(tool.source == "builtin" for tool in request.tools)
    )


@router.get("/servers", response_model=list[McpServerResponse])
async def list_mcp_servers(
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["workspace", "target"] = Query(default="target"),
    _token_ok: None = Depends(require_admin_service_token),
) -> list[McpServerResponse]:
    _validate_registry_scope(scope_type, target_id, target_type)
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
        )
        response.append(_build_server_response(server, server_tools))
    return response


@router.get("/tools", response_model=list[ToolConfigResponse])
async def list_mcp_tools(
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["workspace", "target"] = Query(default="target"),
    include_server_disabled: bool = Query(default=False),
    include_disabled: bool = Query(default=False),
    _token_ok: None = Depends(require_admin_service_token),
) -> list[ToolConfigResponse]:
    _validate_registry_scope(scope_type, target_id, target_type)
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


@router.patch("/tools/{tool_name}", response_model=ToolConfigResponse)
async def update_mcp_tool(
    request: ToolUpdateRequest,
    tool_name: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["workspace", "target"] = Query(default="target"),
    _token_ok: None = Depends(require_admin_service_token),
) -> ToolConfigResponse:
    _validate_registry_scope(scope_type, target_id, target_type)
    existing = await tool_registry.get_tool(
        workspace_id,
        target_id,
        tool_name,
        target_type=target_type,
        include_disabled=True,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    if (
        existing.source == "mcp"
        and not existing.enabled
        and request.enabled is True
        and request.capability is None
    ):
        raise HTTPException(
            status_code=400,
            detail="capability is required when enabling a discovered MCP tool",
        )
    updated = await tool_registry.upsert_tool(
        tool_name=existing.tool_name,
        mcp_server_url=existing.mcp_server_url,
        workspace_id=workspace_id,
        target_id=target_id,
        target_type=existing.target_type,
        timeout_ms=(
            request.timeout_ms if request.timeout_ms is not None else existing.timeout_ms
        ),
        input_schema=(
            request.input_schema
            if request.input_schema is not None
            else existing.input_schema
        ),
        output_schema=(
            request.output_schema
            if request.output_schema is not None
            else existing.output_schema
        ),
        artifact_policy=(
            request.artifact_policy
            if request.artifact_policy is not None
            else getattr(existing, "artifact_policy", "never")
        ),
        enabled=existing.enabled if request.enabled is None else request.enabled,
        description=(
            request.description
            if request.description is not None
            else existing.description
        ),
        capability=request.capability if request.capability is not None else existing.capability,
        version=request.version if request.version is not None else existing.version,
        source=existing.source,
    )
    return _build_tool_response(updated)


@router.post("/servers", response_model=McpServerResponse, status_code=201)
async def create_mcp_server(
    request: McpServerCreateRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    is_builtin_bridge = _is_builtin_bridge_registration(request)
    if not is_builtin_bridge:
        try:
            await validate_mcp_server_url(request.server_url)
        except McpEgressPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    auth_secret_name = request.auth_secret_name
    if request.auth_secret_value:
        auth_secret_name = await _persist_server_secret(
            request.workspace_id,
            request.target_id,
            request.target_type,
            request.server_name,
            request.auth_secret_name,
            request.auth_secret_value,
        )

    if request.auth_type in ("bearer_token", "custom_header") and not auth_secret_name:
        raise HTTPException(
            status_code=400,
            detail="auth_secret_name or auth_secret_value is required for configured auth_type",
        )

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
            auth_secret_name=auth_secret_name,
            auth_header_name=auth_header_name,
            auth_header_prefix=auth_header_prefix,
            public_headers=request.public_headers,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="MCP server name or URL already exists in target",
        ) from exc

    tools_to_apply = request.tools
    discovery_error: str | None = None
    if len(tools_to_apply) == 0:
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
                server_url=request.server_url,
                discovery_error=discovery_error,
            )
            tools_to_apply = []
        except Exception:
            logger.exception(
                "mcp_tool_discovery_failed",
                workspace_id=request.workspace_id,
                target_id=request.target_id,
                server_name=request.server_name,
                server_url=request.server_url,
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
        remove_disabled=len(request.tools) > 0,
    )

    server_tools = await _resolve_tools_for_server(
        request.workspace_id,
        request.target_id,
        request.server_url,
        target_type=request.target_type,
    )
    return _build_server_response(server, server_tools)


@router.patch("/servers/{server_id}", response_model=McpServerResponse)
async def update_mcp_server(
    request: McpServerUpdateRequest,
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["workspace", "target"] = Query(default="target"),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    _validate_registry_scope(scope_type, target_id, target_type)
    server = await mcp_server_registry.get_server(
        workspace_id, target_id, server_id, target_type=target_type
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    next_auth_type = request.auth_type if request.auth_type is not None else server.auth_type
    next_auth_secret_name = (
        request.auth_secret_name
        if request.auth_secret_name is not None
        else server.auth_secret_name
    )
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
            request.auth_secret_name,
            request.auth_secret_value,
            request.auth_header_name,
            request.auth_header_prefix,
        )
    )
    if next_auth_type == "none" and request_has_auth_fields:
        raise HTTPException(
            status_code=400,
            detail="auth fields are not allowed when auth_type is none",
        )
    if next_auth_type in ("bearer_token", "custom_header") and not (
        next_auth_secret_name or request.auth_secret_value
    ):
        raise HTTPException(
            status_code=400,
            detail="auth_secret_name or auth_secret_value is required for configured auth_type",
        )
    if next_auth_type == "custom_header" and not next_auth_header_name:
        raise HTTPException(
            status_code=400,
            detail="auth_header_name is required for custom_header auth",
        )
    if request.auth_secret_value:
        try:
            validate_auth_header_value(
                f"{next_auth_header_prefix or ''}{request.auth_secret_value}"
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    auth_secret_name = request.auth_secret_name
    if request.auth_secret_value:
        auth_secret_name = await _persist_server_secret(
            workspace_id,
            target_id,
            server.target_type,
            request.server_name or server.server_name,
            request.auth_secret_name or server.auth_secret_name,
            request.auth_secret_value,
        )

    patch: dict[str, Any] = {}
    if request.server_name is not None:
        patch["server_name"] = request.server_name
    if request.enabled is not None:
        patch["enabled"] = request.enabled
    if request.auth_type is not None:
        patch["auth_type"] = request.auth_type
    if auth_secret_name is not None:
        patch["auth_secret_name"] = auth_secret_name
    if request.auth_type is not None:
        patch["auth_header_name"] = next_auth_header_name
        patch["auth_header_prefix"] = next_auth_header_prefix
        if request.auth_type == "none":
            patch["auth_secret_name"] = None
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

    if patch:
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
        )
    elif not request.remove_tools:
        # Recovery path: if a server has no tools mapped, try discovery on update.
        current_tools = await _resolve_tools_for_server(
            workspace_id,
            target_id,
            server.server_url,
            target_type=server.target_type,
        )
        if len(current_tools) == 0:
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
                    server_url=server.server_url,
                    discovery_error=discovery_error,
                )
            except Exception:
                logger.exception(
                    "mcp_tool_discovery_failed_on_update",
                    workspace_id=workspace_id,
                    target_id=target_id,
                    server_name=server.server_name,
                    server_url=server.server_url,
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
            )

    server_tools = await _resolve_tools_for_server(
        workspace_id,
        target_id,
        server.server_url,
        target_type=server.target_type,
    )
    return _build_server_response(server, server_tools)


@router.post("/servers/{server_id}/test", response_model=McpServerConnectionTestResponse)
async def test_mcp_server_connection(
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str = Query(..., min_length=1),
    target_type: str = Query(..., min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["workspace", "target"] = Query(default="target"),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerConnectionTestResponse:
    _validate_registry_scope(scope_type, target_id, target_type)
    server = await mcp_server_registry.get_server(
        workspace_id, target_id, server_id, target_type=target_type
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

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
            server_url=server.server_url,
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
    scope_type: Literal["workspace", "target"] = Query(default="target"),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    _validate_registry_scope(scope_type, target_id, target_type)
    server = await mcp_server_registry.get_server(
        workspace_id, target_id, server_id, target_type=target_type
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    server_tools = await _resolve_tools_for_server(
        workspace_id,
        target_id,
        server.server_url,
        target_type=server.target_type,
    )
    for tool in server_tools:
        await tool_registry.remove_tool_for_target(
            tool.tool_name,
            workspace_id,
            target_id,
            target_type=server.target_type,
        )

    if server.auth_secret_name:
        try:
            await secret_store.delete_secret(
                server.auth_secret_name,
                {
                    "workspace_id": workspace_id,
                    "target_id": target_id,
                    "target_type": server.target_type,
                },
            )
        except Exception as exc:
            logger.exception(
                "mcp_server_secret_delete_failed",
                workspace_id=workspace_id,
                scope_type=scope_type,
                server_name=server.server_name,
            )
            raise HTTPException(
                status_code=503,
                detail="MCP credential backend unavailable",
            ) from exc

    deleted = await mcp_server_registry.delete_server(
        workspace_id,
        target_id,
        server_id,
        target_type=target_type,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="MCP server not found")
