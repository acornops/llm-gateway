from __future__ import annotations

from datetime import UTC, datetime
from functools import wraps
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError

from app.api.catalog_bootstrap import (
    ensure_configured_sources as _ensure_configured_sources,
)
from app.api.catalog_endpoint_configuration import resolve_catalog_endpoint
from app.api.handlers_catalog_sources import source_headers as _source_headers
from app.api.handlers_catalog_sources import sync_source as _sync_source
from app.api.handlers_mcp_connections import cleanup_server_connections
from app.api.mcp_admin_helpers import (
    _apply_tools_for_server,
    _build_server_response,
    _discover_server_tools,
    _record_discovery_status,
    _resolve_tools_for_server,
)
from app.api.mcp_admin_schemas import McpServerResponse
from app.auth.service_token import require_admin_service_token
from app.catalog.adapter import CatalogAdapterError, McpRegistryV01Adapter
from app.catalog.models import CatalogArtifact
from app.catalog.schemas import (
    CatalogArtifactListResponse,
    CatalogArtifactResponse,
    CatalogMcpImportRequest,
)
from app.catalog.store import catalog_store
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.observability.metrics import GATEWAY_CATALOG_IMPORTS_TOTAL

router = APIRouter()
logger = structlog.get_logger()


def _track_catalog_import(handler):
    @wraps(handler)
    async def tracked(request: CatalogMcpImportRequest, *args, **kwargs):
        payload = request.root
        operation = "reimport" if payload.reimport_server_id else "import"
        try:
            response = await handler(request, *args, **kwargs)
        except Exception:
            GATEWAY_CATALOG_IMPORTS_TOTAL.labels(
                scope_type=payload.scope_type,
                operation=operation,
                outcome="failure",
            ).inc()
            logger.warning(
                "catalog_mcp_import_failed",
                workspace_id=payload.workspace_id,
                scope_type=payload.scope_type,
                operation=operation,
                outcome="failure",
            )
            raise
        GATEWAY_CATALOG_IMPORTS_TOTAL.labels(
            scope_type=payload.scope_type,
            operation=operation,
            outcome="success",
        ).inc()
        return response

    return tracked


def _artifact_response(artifact: CatalogArtifact) -> CatalogArtifactResponse:
    return CatalogArtifactResponse(
        id=str(artifact.id),
        workspace_id=artifact.workspace_id,
        source_id=str(artifact.source_id),
        binding_id=str(artifact.binding_id),
        artifact_kind=artifact.artifact_kind,
        name=artifact.artifact_name,
        title=artifact.title,
        description=artifact.description,
        version=artifact.version,
        digest=artifact.digest,
        metadata=artifact.metadata_json or {},
        compatible=bool(artifact.compatible),
        incompatibility_reason=artifact.incompatibility_reason,
        remote_endpoints=list(artifact.remote_endpoints or []),
        published_at=artifact.published_at,
        upstream_updated_at=artifact.upstream_updated_at,
    )


@router.get("/artifacts", response_model=CatalogArtifactListResponse)
async def list_catalog_artifacts(
    workspace_id: str = Query(..., min_length=1),
    artifact_kind: Literal["mcp_server", "agent_skill"] = Query(default="mcp_server"),
    source_id: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    compatible: bool | None = Query(default=None),
    refresh: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _token_ok: None = Depends(require_admin_service_token),
) -> CatalogArtifactListResponse:
    await _ensure_configured_sources(workspace_id)
    if refresh and artifact_kind == "mcp_server":
        for source, _bindings in await catalog_store.list_sources(workspace_id):
            if source.enabled and (source_id is None or str(source.id) == source_id):
                await _sync_source(workspace_id, str(source.id))
    items = await catalog_store.list_artifacts(
        workspace_id,
        artifact_kind=artifact_kind,
        source_id=source_id,
        search=search,
        compatible=compatible,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(items) > limit
    return CatalogArtifactListResponse(
        items=[_artifact_response(item) for item in items[:limit]],
        next_cursor=str(offset + limit) if has_more else None,
    )


@router.get("/artifacts/{artifact_id}", response_model=CatalogArtifactResponse)
async def get_catalog_artifact(
    artifact_id: str = Path(...),
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> CatalogArtifactResponse:
    artifact = await catalog_store.get_artifact(workspace_id, artifact_id=artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Catalog artifact not found")
    return _artifact_response(artifact)


@router.post("/imports", response_model=McpServerResponse, status_code=201)
@_track_catalog_import
async def import_catalog_mcp_server(
    request: CatalogMcpImportRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    request = request.root
    if request.scope_type == "agent":
        destination_id = request.agent_id
        registry_target_type = "agent"
        target_constraints = request.target_constraints
    else:
        destination_id = request.target_id
        registry_target_type = request.target_type
        target_constraints = {}
    artifact = await catalog_store.get_artifact(
        request.workspace_id,
        artifact_id=request.artifact.artifact_id,
        source_id=request.artifact.source_id,
        artifact_name=request.artifact.artifact_name,
        version=request.version,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Catalog artifact version not found")
    if artifact.version != request.version:
        pair = await catalog_store.get_source_binding(request.workspace_id, str(artifact.source_id))
        if pair is None:
            raise HTTPException(status_code=404, detail="Catalog source not found")
        source, binding = pair
        try:
            resolved = await McpRegistryV01Adapter(
                source.base_url,
                base_path=binding.adapter_base_path,
                headers=await _source_headers(source),
            ).fetch_artifact(artifact.artifact_name, request.version)
        except CatalogAdapterError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await catalog_store.upsert_artifacts(
            workspace_id=request.workspace_id,
            source_id=source.id,
            binding_id=binding.id,
            artifacts=[resolved],
        )
        artifact = await catalog_store.get_artifact(
            request.workspace_id,
            source_id=str(source.id),
            artifact_name=resolved.name,
            version=resolved.version,
        )
        if artifact is None:
            raise HTTPException(status_code=500, detail="Resolved artifact was not persisted")
    resolved_endpoint_configuration = await resolve_catalog_endpoint(artifact, request)
    resolved_endpoint = resolved_endpoint_configuration.url
    public_headers = resolved_endpoint_configuration.public_headers
    credential_mode = resolved_endpoint_configuration.credential_mode
    credential_header_name = resolved_endpoint_configuration.credential_header_name
    credential_auth_type = resolved_endpoint_configuration.credential_auth_type
    credential_auth_header_prefix = resolved_endpoint_configuration.credential_auth_header_prefix
    requires_credential = credential_mode != "none"
    server_name = request.server_name or artifact.title or artifact.artifact_name
    if not requires_credential:
        require_remote_mcp_enabled()
    reimport_server = None
    if request.reimport_server_id:
        reimport_server = await mcp_server_registry.get_server(
            request.workspace_id,
            destination_id,
            request.reimport_server_id,
            target_type=registry_target_type,
        )
        if reimport_server is None:
            raise HTTPException(status_code=404, detail="MCP installation not found")
        if reimport_server.provenance_type != "catalog":
            raise HTTPException(
                status_code=409,
                detail="Only catalog installations can be explicitly reimported",
            )
        if (
            reimport_server.catalog_source_id != artifact.source_id
            or reimport_server.catalog_artifact_name != artifact.artifact_name
        ):
            raise HTTPException(
                status_code=409,
                detail="Catalog reimport provenance does not match this installation",
            )
    existing = await mcp_server_registry.get_server_by_url(
        request.workspace_id,
        destination_id,
        resolved_endpoint,
        target_type=registry_target_type,
        enabled_only=False,
    )
    if existing is not None:
        if reimport_server is not None and existing.id != reimport_server.id:
            raise HTTPException(
                status_code=409,
                detail="Selected endpoint is owned by another installation in this destination",
            )
        if reimport_server is not None:
            existing = None
        else:
            if (
                existing.catalog_source_id == artifact.source_id
                and existing.catalog_artifact_name == artifact.artifact_name
                and existing.catalog_version == artifact.version
                and existing.catalog_digest == artifact.digest
            ):
                tools = await _resolve_tools_for_server(
                    request.workspace_id,
                    destination_id,
                    existing.server_url,
                    target_type=registry_target_type,
                    server_id=str(existing.id),
                )
                return _build_server_response(existing, tools)
            raise HTTPException(
                status_code=409,
                detail=(
                    "An installation already owns this endpoint; use explicit "
                    "reimport to upgrade it"
                ),
            )
    try:
        if reimport_server is not None:
            if (
                request.expected_revision is not None
                and reimport_server.revision != request.expected_revision
            ):
                raise HTTPException(status_code=409, detail="MCP server revision does not match")
            trust_changed = any(
                (
                    reimport_server.server_url != resolved_endpoint,
                    reimport_server.auth_type
                    != (credential_auth_type if requires_credential else "none"),
                    reimport_server.auth_header_name
                    != (credential_header_name if requires_credential else None),
                    reimport_server.auth_header_prefix
                    != (credential_auth_header_prefix if requires_credential else None),
                    reimport_server.credential_mode != credential_mode,
                    (reimport_server.public_headers or {}) != (public_headers or {}),
                    reimport_server.catalog_digest != artifact.digest,
                )
            )
            server_patch = {
                "server_name": server_name,
                "server_url": resolved_endpoint,
                "enabled": request.enabled,
                "auth_type": credential_auth_type if requires_credential else "none",
                "auth_header_name": credential_header_name if requires_credential else None,
                "auth_header_prefix": (
                    credential_auth_header_prefix if requires_credential else None
                ),
                "credential_mode": credential_mode,
                "public_headers": public_headers or None,
                "catalog_source_id": artifact.source_id,
                "catalog_artifact_name": artifact.artifact_name,
                "catalog_version": artifact.version,
                "catalog_digest": artifact.digest,
                "catalog_imported_at": datetime.now(UTC),
                "provenance_type": "catalog",
                "endpoint_configuration": request.endpoint_configuration,
                "target_constraints": target_constraints,
                "connection_status": "unknown",
                "last_discovery_at": None,
                "last_discovery_error": None,
            }
            if trust_changed:
                transitioning = await mcp_server_registry.update_server(
                    request.workspace_id,
                    destination_id,
                    str(reimport_server.id),
                    {
                        "expected_revision": request.expected_revision,
                        "credential_transitioning": True,
                        "connection_status": "error",
                        "last_discovery_at": None,
                        "last_discovery_error": ("Credential configuration update in progress."),
                    },
                    target_type=registry_target_type,
                )
                if transitioning is None:
                    raise HTTPException(status_code=404, detail="MCP installation not found")
                await cleanup_server_connections(
                    request.workspace_id,
                    str(reimport_server.id),
                    reason=(
                        "mode_transition"
                        if reimport_server.credential_mode != credential_mode
                        else "trust_change"
                    ),
                )
                logger.info(
                    "mcp_connections_invalidated_for_trust_change",
                    workspace_id=request.workspace_id,
                    scope_type=request.scope_type,
                    server_id=str(reimport_server.id),
                )
                server_patch["credential_transitioning"] = False
            else:
                server_patch["expected_revision"] = request.expected_revision
            server = await mcp_server_registry.update_server(
                request.workspace_id,
                destination_id,
                str(reimport_server.id),
                server_patch,
                target_type=registry_target_type,
            )
            if server is None:
                raise HTTPException(status_code=404, detail="MCP installation not found")
        else:
            server = await mcp_server_registry.create_server(
                workspace_id=request.workspace_id,
                target_id=destination_id,
                target_type=registry_target_type,
                server_name=server_name,
                server_url=resolved_endpoint,
                enabled=request.enabled,
                auth_type=credential_auth_type if requires_credential else "none",
                auth_header_name=credential_header_name if requires_credential else None,
                auth_header_prefix=(credential_auth_header_prefix if requires_credential else None),
                credential_mode=credential_mode,
                public_headers=public_headers or None,
                catalog_source_id=str(artifact.source_id),
                catalog_artifact_name=artifact.artifact_name,
                catalog_version=artifact.version,
                catalog_digest=artifact.digest,
                catalog_imported_at=datetime.now(UTC),
                provenance_type="catalog",
                endpoint_configuration=request.endpoint_configuration,
                target_constraints=target_constraints,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="MCP server name or endpoint already exists in destination"
        ) from exc

    discovery_error: str | None = None
    tools = []
    if requires_credential:
        discovery_error = "A credential connection is required before tool discovery."
    else:
        try:
            tools, discovery_error = await _discover_server_tools(
                request.workspace_id, destination_id, server
            )
        except Exception:
            discovery_error = "MCP server discovery failed."
    await _apply_tools_for_server(
        request.workspace_id,
        destination_id,
        server.server_url,
        tools,
        target_type=registry_target_type,
        server_id=str(server.id),
        remove_disabled=False,
    )
    if reimport_server is not None and discovery_error is None:
        await tool_registry.remove_server_tools_not_in(
            request.workspace_id,
            destination_id,
            registry_target_type,
            str(server.id),
            {tool.name for tool in tools},
        )
    updated = await _record_discovery_status(
        request.workspace_id,
        destination_id,
        str(server.id),
        discovery_error,
        target_type=registry_target_type,
    )
    server = updated or server
    server_tools = await _resolve_tools_for_server(
        request.workspace_id,
        destination_id,
        server.server_url,
        target_type=registry_target_type,
        server_id=str(server.id),
    )
    logger.info(
        "catalog_mcp_reimported" if reimport_server is not None else "catalog_mcp_imported",
        workspace_id=request.workspace_id,
        scope_type=request.scope_type,
        operation="reimport" if reimport_server is not None else "import",
        outcome="success",
    )
    return _build_server_response(server, server_tools)
