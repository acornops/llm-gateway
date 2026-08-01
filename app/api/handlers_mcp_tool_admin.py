from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.handlers_mcp_connections import cleanup_server_connections
from app.api.mcp_admin_helpers import _build_tool_response, _resolve_tools_for_server
from app.api.mcp_admin_schemas import ToolConfigResponse, ToolUpdateRequest
from app.api.mcp_admin_validation import registry_destination, registry_scope_options
from app.auth.service_token import require_admin_service_token
from app.examples import EXAMPLE_MCP_SERVER_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.target_types import TARGET_TYPE_EXAMPLES

router = APIRouter()


@router.patch(
    "/tools/{tool_name}",
    response_model=ToolConfigResponse,
    response_model_exclude_none=True,
)
async def update_mcp_tool(
    request: ToolUpdateRequest,
    tool_name: str = Path(..., min_length=1),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    server_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> ToolConfigResponse:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    registry_scope = registry_scope_options(scope_type, destination_target_type)
    existing = await tool_registry.get_tool(
        workspace_id,
        destination_id,
        tool_name,
        include_disabled=True,
        server_id=server_id,
        **registry_scope,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    review_state = request.review_state or getattr(existing, "review_state", "pending")
    target_approval = (
        scope_type == "target"
        and existing.source == "mcp"
        and request.enabled is True
        and review_state != "approved"
    )
    if target_approval:
        if request.capability is None:
            raise HTTPException(
                status_code=400,
                detail="capability is required when enabling a discovered MCP tool",
            )
        review_state = "approved"
    if existing.source == "mcp" and request.enabled is True and review_state != "approved":
        raise HTTPException(
            status_code=400,
            detail="MCP tools must be approved before they are enabled",
        )
    next_risk = request.risk_level or (
        "read_only"
        if target_approval and request.capability == "read"
        else getattr(existing, "risk_level", "high_risk")
    )
    next_auto_allowed = (
        request.auto_allowed
        if request.auto_allowed is not None
        else bool(getattr(existing, "auto_allowed", False))
    )
    if next_auto_allowed and next_risk != "non_destructive_write":
        raise HTTPException(
            status_code=400,
            detail="only approved non-destructive writes may be auto allowed",
        )
    updated = await tool_registry.upsert_tool(
        tool_name=existing.tool_name,
        mcp_server_url=existing.mcp_server_url,
        workspace_id=workspace_id,
        destination_id=destination_id,
        timeout_ms=request.timeout_ms if request.timeout_ms is not None else existing.timeout_ms,
        input_schema=(
            request.input_schema if request.input_schema is not None else existing.input_schema
        ),
        output_schema=(
            request.output_schema if request.output_schema is not None else existing.output_schema
        ),
        artifact_policy=(
            request.artifact_policy
            if request.artifact_policy is not None
            else getattr(existing, "artifact_policy", "never")
        ),
        enabled=existing.enabled if request.enabled is None else request.enabled,
        description=(
            request.description if request.description is not None else existing.description
        ),
        capability=request.capability if request.capability is not None else existing.capability,
        version=request.version if request.version is not None else existing.version,
        source=existing.source,
        server_id=str(existing.server_id),
        review_state=review_state,
        risk_level=next_risk,
        auto_allowed=next_auto_allowed,
        **registry_scope,
    )
    return _build_tool_response(updated)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_mcp_server(
    server_id: str = Path(..., examples=[EXAMPLE_MCP_SERVER_ID]),
    workspace_id: str = Query(..., min_length=1, examples=[EXAMPLE_WORKSPACE_ID]),
    target_id: str | None = Query(default=None, min_length=1),
    target_type: str | None = Query(default=None, min_length=1, examples=TARGET_TYPE_EXAMPLES),
    scope_type: Literal["agent", "target"] = Query(default="target"),
    agent_id: str | None = Query(default=None),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    destination_id, destination_target_type = registry_destination(
        scope_type, target_id, target_type, agent_id
    )
    registry_scope = registry_scope_options(scope_type, destination_target_type)
    server = await mcp_server_registry.get_server(
        workspace_id, destination_id, server_id, **registry_scope
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    await cleanup_server_connections(workspace_id, server_id)
    server_tools = await _resolve_tools_for_server(
        workspace_id,
        destination_id,
        server.server_url,
        server_id=str(server.id),
        **registry_scope,
    )
    for tool in server_tools:
        await tool_registry.remove_tool(
            tool.tool_name,
            workspace_id,
            destination_id,
            server_id=str(server.id),
            **registry_scope,
        )
    if not await mcp_server_registry.delete_server(
        workspace_id, destination_id, server_id, **registry_scope
    ):
        raise HTTPException(status_code=404, detail="MCP server not found")
