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
from app.api.catalog_mcp_reconciliation import reimport_catalog_server_locked
from app.api.handlers_catalog_sources import source_headers as _source_headers
from app.api.handlers_catalog_sources import sync_source as _sync_source
from app.api.mcp_admin_helpers import (
    _apply_tools_for_server,
    _build_server_response,
    _discover_server_tools,
    _record_discovery_status,
    _resolve_tools_for_server,
)
from app.api.mcp_admin_schemas import McpServerResponse
from app.api.mcp_admin_validation import registry_scope_options
from app.api.mcp_connection_cleanup import cleanup_server_connections
from app.api.mcp_lifecycle_guard import (
    guarded_destination_operation,
    guarded_server_operation,
    guarded_workspace_mutation,
)
from app.auth.service_token import require_admin_service_token
from app.catalog.adapter import CatalogAdapterError, McpRegistryV01Adapter
from app.catalog.models import CatalogArtifact
from app.catalog.schemas import (
    CatalogArtifactListResponse,
    CatalogArtifactResponse,
    CatalogMcpImportBase,
    CatalogMcpImportRequest,
)
from app.catalog.store import catalog_store
from app.mcp.lifecycle import McpDestination
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


def _catalog_source_authority_snapshot(
    source: object,
    binding: object,
) -> tuple[object, ...]:
    return (
        str(source.id),
        int(vars(source).get("authority_generation", 1) or 1),
        bool(source.enabled),
        bool(vars(source).get("credential_transitioning", False)),
        str(binding.id),
    )


def _catalog_import_authority_snapshot(
    source: object,
    binding: object,
    artifact: CatalogArtifact,
) -> tuple[object, ...]:
    return (
        *_catalog_source_authority_snapshot(source, binding),
        str(artifact.id),
        str(artifact.source_id),
        str(artifact.binding_id),
        artifact.artifact_name,
        artifact.version,
        artifact.digest,
    )


async def _load_catalog_import_authority(
    workspace_id: str,
    artifact: CatalogArtifact,
) -> tuple[object, object, tuple[object, ...]]:
    pair = await catalog_store.get_source_binding(
        workspace_id,
        str(artifact.source_id),
    )
    if pair is None:
        raise HTTPException(status_code=409, detail="Catalog source is no longer available")
    source, binding = pair
    if not bool(source.enabled) or bool(
        vars(source).get("credential_transitioning", False)
    ):
        raise HTTPException(status_code=409, detail="Catalog source is not importable")
    if str(binding.id) != str(artifact.binding_id):
        raise HTTPException(status_code=409, detail="Catalog artifact authority changed")
    return source, binding, _catalog_import_authority_snapshot(source, binding, artifact)


async def _revalidate_catalog_import_authority(
    workspace_id: str,
    artifact_id: str,
    expected: tuple[object, ...],
) -> CatalogArtifact:
    current = await catalog_store.get_artifact(workspace_id, artifact_id=artifact_id)
    if current is None:
        raise HTTPException(status_code=409, detail="Catalog artifact is no longer available")
    _source, _binding, current_snapshot = await _load_catalog_import_authority(
        workspace_id,
        current,
    )
    if current_snapshot != expected:
        raise HTTPException(status_code=409, detail="Catalog import authority changed")
    return current


async def _reimport_catalog_server_locked(
    *,
    request: CatalogMcpImportBase,
    artifact: CatalogArtifact,
    current: object,
    destination_id: str,
    registry_scope: dict[str, object],
    resolved_endpoint: str,
    server_name: str,
    requires_credential: bool,
    credential_mode: str,
    credential_auth_type: str | None,
    credential_header_name: str | None,
    credential_auth_header_prefix: str | None,
    public_headers: dict[str, str] | None,
) -> tuple[object, bool]:
    return await reimport_catalog_server_locked(
        request=request,
        artifact=artifact,
        current=current,
        destination_id=destination_id,
        registry_scope=registry_scope,
        resolved_endpoint=resolved_endpoint,
        server_name=server_name,
        requires_credential=requires_credential,
        credential_mode=credential_mode,
        credential_auth_type=credential_auth_type,
        credential_header_name=credential_header_name,
        credential_auth_header_prefix=credential_auth_header_prefix,
        public_headers=public_headers,
        server_registry=mcp_server_registry,
        tool_registry=tool_registry,
        cleanup_server_connections=cleanup_server_connections,
        logger=logger,
    )


async def _finalize_catalog_server_locked(
    *,
    request: CatalogMcpImportBase,
    server: object,
    destination_id: str,
    registry_scope: dict[str, object],
    requires_credential: bool,
    reimport: bool,
    credential_free_reconciliation: bool,
) -> McpServerResponse:
    """Discover and commit authority while the canonical server guard is held."""

    discovery_error: str | None = None
    tools = []
    if requires_credential:
        discovery_error = "A credential connection is required before tool discovery."
    else:
        try:
            tools, discovery_error, _discovery_error_code = await _discover_server_tools(
                request.workspace_id, destination_id, server
            )
        except Exception:
            discovery_error = "MCP server discovery failed."
    await _apply_tools_for_server(
        request.workspace_id,
        destination_id,
        tools,
        server_id=str(server.id),
        remove_disabled=False,
        **registry_scope,
    )
    if reimport and discovery_error is None:
        await tool_registry.remove_server_tools_not_in(
            request.workspace_id,
            destination_id,
            server_id=str(server.id),
            tool_names={tool.name for tool in tools},
            **registry_scope,
        )
    updated = await _record_discovery_status(
        request.workspace_id,
        destination_id,
        str(server.id),
        discovery_error,
        **registry_scope,
    )
    server = updated or server
    if credential_free_reconciliation and discovery_error is not None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Catalog MCP trust update remains fenced because tool discovery "
                "failed; retry this reimport"
            ),
        )
    if credential_free_reconciliation:
        reconciled = await mcp_server_registry.update_server(
            request.workspace_id,
            destination_id,
            str(server.id),
            {"credential_transitioning": False},
            **registry_scope,
        )
        if reconciled is None:
            raise HTTPException(status_code=404, detail="MCP installation not found")
        server = reconciled
    server_tools = await _resolve_tools_for_server(
        request.workspace_id,
        destination_id,
        server_id=str(server.id),
        **registry_scope,
    )
    logger.info(
        "catalog_mcp_reimported" if reimport else "catalog_mcp_imported",
        workspace_id=request.workspace_id,
        scope_type=request.scope_type,
        operation="reimport" if reimport else "import",
        outcome="success",
    )
    return _build_server_response(server, server_tools)


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


@router.post(
    "/imports",
    response_model=McpServerResponse,
    response_model_exclude_none=True,
    status_code=201,
)
@_track_catalog_import
async def import_catalog_mcp_server(
    request: CatalogMcpImportRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpServerResponse:
    request = request.root
    async with guarded_workspace_mutation(request.workspace_id):
        pass
    if request.scope_type == "agent":
        destination_id = request.agent_id
        destination_target_type = None
        registry_scope = registry_scope_options("agent")
    else:
        destination_id = request.target_id
        destination_target_type = request.target_type
        registry_scope = registry_scope_options("target", request.target_type)
    destination = McpDestination(
        workspace_id=request.workspace_id,
        scope_type=request.scope_type,
        destination_id=destination_id,
        target_type=destination_target_type,
    )
    artifact = await catalog_store.get_artifact(
        request.workspace_id,
        artifact_id=request.artifact.artifact_id,
        source_id=request.artifact.source_id,
        artifact_name=request.artifact.artifact_name,
        version=request.version,
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Catalog artifact version not found")
    source, binding, import_authority = await _load_catalog_import_authority(
        request.workspace_id,
        artifact,
    )
    if artifact.version != request.version:
        source_authority = _catalog_source_authority_snapshot(source, binding)
        try:
            resolved = await McpRegistryV01Adapter(
                source.base_url,
                base_path=binding.adapter_base_path,
                headers=await _source_headers(source),
            ).fetch_artifact(artifact.artifact_name, request.version)
        except CatalogAdapterError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if (
            resolved.name != artifact.artifact_name
            or resolved.version != request.version
        ):
            raise HTTPException(
                status_code=502,
                detail="Catalog registry returned a different artifact identity",
            )
        async with guarded_workspace_mutation(request.workspace_id):
            current_pair = await catalog_store.get_source_binding(
                request.workspace_id,
                str(source.id),
            )
            if (
                current_pair is None
                or _catalog_source_authority_snapshot(*current_pair)
                != source_authority
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Catalog source changed while the version was fetched",
                )
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
        source, binding, import_authority = await _load_catalog_import_authority(
            request.workspace_id,
            artifact,
        )
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
    if request.reimport_server_id:
        try:
            async with guarded_workspace_mutation(request.workspace_id):
                artifact = await _revalidate_catalog_import_authority(
                    request.workspace_id,
                    str(artifact.id),
                    import_authority,
                )
            async with guarded_server_operation(
                request.workspace_id,
                request.reimport_server_id,
                allow_transitioning=True,
            ) as current:
                server, credential_free_reconciliation = (
                    await _reimport_catalog_server_locked(
                        request=request,
                        artifact=artifact,
                        current=current,
                        destination_id=destination_id,
                        registry_scope=registry_scope,
                        resolved_endpoint=resolved_endpoint,
                        server_name=server_name,
                        requires_credential=requires_credential,
                        credential_mode=credential_mode,
                        credential_auth_type=credential_auth_type,
                        credential_header_name=credential_header_name,
                        credential_auth_header_prefix=credential_auth_header_prefix,
                        public_headers=public_headers,
                    )
                )
                return await _finalize_catalog_server_locked(
                    request=request,
                    server=server,
                    destination_id=destination_id,
                    registry_scope=registry_scope,
                    requires_credential=requires_credential,
                    reimport=True,
                    credential_free_reconciliation=credential_free_reconciliation,
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="MCP server name or endpoint already exists in destination",
            ) from exc

    created = False
    try:
        async with guarded_destination_operation(destination):
            artifact = await _revalidate_catalog_import_authority(
                request.workspace_id,
                str(artifact.id),
                import_authority,
            )
            existing = await mcp_server_registry.get_server_by_url(
                request.workspace_id,
                destination_id,
                resolved_endpoint,
                enabled_only=False,
                **registry_scope,
            )
            if existing is not None:
                if not all(
                    (
                        existing.provenance_type == "catalog",
                        existing.catalog_source_id == artifact.source_id,
                        existing.catalog_artifact_name == artifact.artifact_name,
                        existing.catalog_version == artifact.version,
                        existing.catalog_digest == artifact.digest,
                    )
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "An installation already owns this endpoint; use explicit "
                            "reimport to upgrade it"
                        ),
                    )
                server_id = str(existing.id)
            else:
                server = await mcp_server_registry.create_server(
                    workspace_id=request.workspace_id,
                    destination_id=destination_id,
                    server_name=server_name,
                    server_url=resolved_endpoint,
                    enabled=request.enabled,
                    auth_type=(
                        credential_auth_type if requires_credential else "none"
                    ),
                    auth_header_name=(
                        credential_header_name if requires_credential else None
                    ),
                    auth_header_prefix=(
                        credential_auth_header_prefix if requires_credential else None
                    ),
                    credential_mode=credential_mode,
                    public_headers=public_headers or None,
                    catalog_source_id=str(artifact.source_id),
                    catalog_artifact_name=artifact.artifact_name,
                    catalog_version=artifact.version,
                    catalog_digest=artifact.digest,
                    catalog_imported_at=datetime.now(UTC),
                    provenance_type="catalog",
                    endpoint_configuration=request.endpoint_configuration,
                    **registry_scope,
                )
                server_id = str(server.id)
                created = True
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="MCP server name or endpoint already exists in destination",
        ) from exc

    async with guarded_server_operation(
        request.workspace_id,
        server_id,
        allow_transitioning=True,
    ) as current:
        if not created:
            if not all(
                (
                    getattr(current, "provenance_type", None) == "catalog",
                    getattr(current, "server_url", None) == resolved_endpoint,
                    getattr(current, "catalog_source_id", None) == artifact.source_id,
                    getattr(current, "catalog_artifact_name", None)
                    == artifact.artifact_name,
                    getattr(current, "catalog_version", None) == artifact.version,
                    getattr(current, "catalog_digest", None) == artifact.digest,
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Catalog installation changed while import was prepared",
                )
            tools = await _resolve_tools_for_server(
                request.workspace_id,
                destination_id,
                server_id=server_id,
                **registry_scope,
            )
            return _build_server_response(current, tools)
        return await _finalize_catalog_server_locked(
            request=request,
            server=current,
            destination_id=destination_id,
            registry_scope=registry_scope,
            requires_credential=requires_credential,
            reimport=False,
            credential_free_reconciliation=False,
        )
