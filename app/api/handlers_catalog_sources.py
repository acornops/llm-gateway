from __future__ import annotations

import uuid
from contextlib import suppress

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import IntegrityError

from app.api.catalog_bootstrap import ensure_configured_sources
from app.auth.service_token import require_admin_service_token
from app.catalog.adapter import CatalogAdapterError, McpRegistryV01Adapter
from app.catalog.models import CatalogBinding, CatalogSource
from app.catalog.schemas import (
    CatalogBindingResponse,
    CatalogSourceCapabilities,
    CatalogSourceCreateRequest,
    CatalogSourceListResponse,
    CatalogSourcePatchRequest,
    CatalogSourceResponse,
)
from app.catalog.store import catalog_store
from app.config.settings import settings
from app.observability.metrics import (
    GATEWAY_CATALOG_CONNECTOR_HEALTH,
    GATEWAY_CATALOG_SYNCHRONIZATIONS_TOTAL,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store

router = APIRouter()
logger = structlog.get_logger()


def _binding_response(binding: CatalogBinding) -> CatalogBindingResponse:
    return CatalogBindingResponse(
        id=str(binding.id),
        artifact_kind=binding.artifact_kind,
        adapter_type=binding.adapter_type,
        adapter_base_path=binding.adapter_base_path,
        sync_status=(
            binding.sync_status
            if binding.sync_status in {"pending", "syncing", "ready", "error"}
            else "error"
        ),
        last_sync_at=binding.last_sync_at,
        last_sync_error=binding.last_sync_error,
    )


def _source_response(
    source: CatalogSource, bindings: list[CatalogBinding]
) -> CatalogSourceResponse:
    return CatalogSourceResponse(
        id=str(source.id),
        workspace_id=source.workspace_id,
        display_name=source.display_name,
        base_url=source.base_url,
        auth_type=source.auth_type,
        credential_configured=bool(source.auth_secret_name),
        auth_header_name=source.auth_header_name,
        network_route=source.network_route,
        enabled=bool(source.enabled),
        management_mode=source.management_mode,
        bindings=[_binding_response(binding) for binding in bindings],
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


async def source_headers(source: CatalogSource) -> dict[str, str]:
    if source.auth_type == "none":
        return {}
    if not source.auth_secret_name:
        raise HTTPException(
            status_code=409, detail="Catalog source credential is not configured"
        )
    try:
        credential = await secret_store.get_secret(
            source.auth_secret_name, {"workspace_id": source.workspace_id}
        )
    except SecretNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="Catalog source credential is not configured"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Catalog credential backend is unavailable"
        ) from exc
    header_name = (
        "Authorization"
        if source.auth_type == "bearer_token"
        else source.auth_header_name
    )
    if not header_name:
        raise HTTPException(
            status_code=409, detail="Catalog source auth header is invalid"
        )
    return {
        header_name: (
            f"Bearer {credential}"
            if source.auth_type == "bearer_token"
            else credential
        )
    }


async def sync_source(
    workspace_id: str, source_id: str, *, incremental: bool = True
) -> int:
    pair = await catalog_store.get_source_binding(workspace_id, source_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Catalog source not found")
    source, binding = pair
    if not source.enabled:
        raise HTTPException(status_code=409, detail="Catalog source is disabled")
    if source.network_route == "connector":
        GATEWAY_CATALOG_CONNECTOR_HEALTH.set(0)
    try:
        synchronized = await catalog_store.sync_mcp_binding(
            source,
            binding,
            headers=await source_headers(source),
            incremental=incremental,
        )
        GATEWAY_CATALOG_SYNCHRONIZATIONS_TOTAL.labels(
            adapter=binding.adapter_type,
            network_route=source.network_route,
            status="success",
        ).inc()
        return synchronized
    except (CatalogAdapterError, ValueError) as exc:
        GATEWAY_CATALOG_SYNCHRONIZATIONS_TOTAL.labels(
            adapter=binding.adapter_type,
            network_route=source.network_route,
            status="failure",
        ).inc()
        logger.warning(
            "catalog_sync_failed",
            workspace_id=workspace_id,
            source_id=source_id,
            adapter_type=binding.adapter_type,
            error_code="CATALOG_SYNC_FAILED",
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sources", response_model=CatalogSourceListResponse)
async def list_catalog_sources(
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> CatalogSourceListResponse:
    await ensure_configured_sources(workspace_id)
    return CatalogSourceListResponse(
        items=[
            _source_response(source, bindings)
            for source, bindings in await catalog_store.list_sources(workspace_id)
        ],
        capabilities=CatalogSourceCapabilities(
            workspace_managed_sources_enabled=(
                settings.CATALOG_WORKSPACE_MANAGED_SOURCES_ENABLED
            )
        ),
    )


@router.post("/sources", response_model=CatalogSourceResponse, status_code=201)
async def create_catalog_source(
    request: CatalogSourceCreateRequest,
    _token_ok: None = Depends(require_admin_service_token),
) -> CatalogSourceResponse:
    if request.management_mode != "workspace":
        raise HTTPException(
            status_code=400,
            detail="Deployment-managed sources must be configured by the deployment",
        )
    if not settings.CATALOG_WORKSPACE_MANAGED_SOURCES_ENABLED:
        raise HTTPException(
            status_code=403, detail="Workspace-managed catalog sources are disabled"
        )
    if request.network_route == "connector":
        raise HTTPException(
            status_code=400,
            detail="Connector-routed catalog sources are not available",
        )
    if request.adapter_base_path != "/v0.1":
        raise HTTPException(
            status_code=400,
            detail="Only the mcp_registry_v0_1 /v0.1 adapter is available",
        )
    secret_name = request.auth_secret_name
    if request.auth_secret_value:
        secret_name = secret_name or f"catalog_source::{uuid.uuid4()}"
    probe_headers: dict[str, str] = {}
    if request.auth_secret_value:
        header_name = (
            "Authorization"
            if request.auth_type == "bearer_token"
            else request.auth_header_name
        )
        if header_name:
            probe_headers[header_name] = (
                f"Bearer {request.auth_secret_value}"
                if request.auth_type == "bearer_token"
                else request.auth_secret_value
            )
    if request.enabled:
        try:
            await McpRegistryV01Adapter(
                request.base_url,
                base_path=request.adapter_base_path,
                headers=probe_headers,
            ).probe()
        except CatalogAdapterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    created_secret = bool(request.auth_secret_value and secret_name)
    if request.auth_secret_value and secret_name:
        await secret_store.put_secret(
            secret_name,
            request.auth_secret_value,
            {"workspace_id": request.workspace_id},
        )
    try:
        source, binding = await catalog_store.create_source(
            workspace_id=request.workspace_id,
            display_name=request.display_name,
            base_url=request.base_url,
            auth_type=request.auth_type,
            auth_secret_name=secret_name,
            auth_header_name=request.auth_header_name,
            network_route=request.network_route,
            enabled=request.enabled,
            management_mode=request.management_mode,
            artifact_kind=request.artifact_kind,
            adapter_type=request.adapter_type,
            adapter_base_path=request.adapter_base_path,
        )
    except IntegrityError as exc:
        if created_secret and secret_name:
            with suppress(Exception):
                await secret_store.delete_secret(
                    secret_name, {"workspace_id": request.workspace_id}
                )
        raise HTTPException(
            status_code=409, detail="Catalog source display name already exists"
        ) from exc
    if source.enabled:
        with suppress(HTTPException):
            await sync_source(
                request.workspace_id, str(source.id), incremental=False
            )
        refreshed = await catalog_store.get_source_binding(
            request.workspace_id, str(source.id), request.artifact_kind
        )
        if refreshed:
            source, binding = refreshed
    logger.info(
        "catalog_source_created",
        workspace_id=request.workspace_id,
        source_id=str(source.id),
        management_mode=source.management_mode,
        enabled=bool(source.enabled),
    )
    return _source_response(source, [binding])


@router.patch("/sources/{source_id}", response_model=CatalogSourceResponse)
async def update_catalog_source(
    request: CatalogSourcePatchRequest,
    source_id: str = Path(...),
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> CatalogSourceResponse:
    pair = await catalog_store.get_source_binding(workspace_id, source_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Catalog source not found")
    source, binding = pair
    if source.management_mode == "bootstrap":
        raise HTTPException(
            status_code=409,
            detail="Deployment-managed catalog sources are configuration read-only",
        )

    auth_was_supplied = "auth" in request.model_fields_set
    next_base_url = request.base_url or source.base_url
    next_auth_type = source.auth_type
    next_auth_header_name = source.auth_header_name
    next_secret_name = source.auth_secret_name
    replacement_credential: str | None = None
    probe_headers: dict[str, str] = {}
    if auth_was_supplied:
        assert request.auth is not None
        next_auth_type = request.auth.type
        if request.auth.type == "none":
            next_auth_header_name = None
            next_secret_name = None
            probe_headers = {}
        else:
            replacement_credential = request.auth.credential
            assert replacement_credential is not None
            next_auth_header_name = (
                request.auth.header_name
                if request.auth.type == "custom_header"
                else None
            )
            header_name = next_auth_header_name or "Authorization"
            probe_headers = {
                header_name: (
                    f"Bearer {replacement_credential}"
                    if request.auth.type == "bearer_token"
                    else replacement_credential
                )
            }
    configuration_changed = any(
        (
            request.base_url is not None and request.base_url != source.base_url,
            auth_was_supplied,
        )
    )
    enabling = request.enabled is True and not source.enabled
    if configuration_changed or enabling:
        if not auth_was_supplied:
            probe_headers = await source_headers(source)
        try:
            await McpRegistryV01Adapter(
                next_base_url,
                base_path=binding.adapter_base_path,
                headers=probe_headers,
            ).probe()
        except CatalogAdapterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    old_secret_name = source.auth_secret_name
    if replacement_credential is not None:
        next_secret_name = f"catalog_source::{uuid.uuid4()}"
        await secret_store.put_secret(
            next_secret_name,
            replacement_credential,
            {"workspace_id": workspace_id},
        )

    changes: dict[str, object] = {}
    if request.display_name is not None:
        changes["display_name"] = request.display_name
    if request.base_url is not None:
        changes["base_url"] = request.base_url
    if request.enabled is not None:
        changes["enabled"] = request.enabled
    if request.network_route is not None:
        changes["network_route"] = request.network_route
    if auth_was_supplied:
        changes.update(
            {
                "auth_type": next_auth_type,
                "auth_header_name": next_auth_header_name,
                "auth_secret_name": next_secret_name,
            }
        )
    try:
        updated = await catalog_store.update_source(
            workspace_id,
            source_id,
            changes,
            clear_artifacts=configuration_changed,
        )
    except IntegrityError as exc:
        if next_secret_name and next_secret_name != old_secret_name:
            with suppress(Exception):
                await secret_store.delete_secret(
                    next_secret_name, {"workspace_id": workspace_id}
                )
        raise HTTPException(
            status_code=409, detail="Catalog source display name already exists"
        ) from exc
    if updated is None:
        if next_secret_name and next_secret_name != old_secret_name:
            with suppress(Exception):
                await secret_store.delete_secret(
                    next_secret_name, {"workspace_id": workspace_id}
                )
        raise HTTPException(status_code=404, detail="Catalog source not found")
    source, binding = updated

    if old_secret_name and old_secret_name != source.auth_secret_name:
        try:
            await secret_store.delete_secret(
                old_secret_name, {"workspace_id": workspace_id}
            )
        except Exception as exc:
            logger.exception(
                "catalog_source_old_credential_delete_failed",
                workspace_id=workspace_id,
                source_id=source_id,
                error_code="CATALOG_SOURCE_SECRET_DELETE_FAILED",
            )
            raise HTTPException(
                status_code=503, detail="Catalog credential backend is unavailable"
            ) from exc

    if configuration_changed and source.enabled:
        await sync_source(workspace_id, source_id, incremental=False)
        refreshed = await catalog_store.get_source_binding(workspace_id, source_id)
        if refreshed:
            source, binding = refreshed
    logger.info(
        "catalog_source_updated",
        workspace_id=workspace_id,
        source_id=source_id,
        enabled=bool(source.enabled),
        configuration_changed=configuration_changed,
    )
    return _source_response(source, [binding])


@router.post("/sources/{source_id}/sync")
async def sync_catalog_source(
    source_id: str = Path(...),
    workspace_id: str = Query(..., min_length=1),
    full: bool = Query(default=False),
    _token_ok: None = Depends(require_admin_service_token),
) -> dict[str, int]:
    artifact_count = await sync_source(
        workspace_id, source_id, incremental=not full
    )
    logger.info(
        "catalog_source_synchronized",
        workspace_id=workspace_id,
        source_id=source_id,
        full=full,
        artifact_count=artifact_count,
    )
    return {"artifact_count": artifact_count}


@router.delete("/sources/{source_id}", status_code=204)
async def delete_catalog_source(
    source_id: str = Path(...),
    workspace_id: str = Query(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> None:
    pair = await catalog_store.get_source_binding(workspace_id, source_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Catalog source not found")
    source, _binding = pair
    if source.management_mode == "bootstrap":
        raise HTTPException(
            status_code=409,
            detail="Deployment-managed catalog sources cannot be deleted",
        )
    if source.auth_secret_name:
        try:
            await secret_store.delete_secret(
                source.auth_secret_name, {"workspace_id": workspace_id}
            )
        except Exception as exc:
            logger.exception(
                "catalog_source_credential_delete_failed",
                workspace_id=workspace_id,
                source_id=source_id,
                error_code="CATALOG_SOURCE_SECRET_DELETE_FAILED",
            )
            raise HTTPException(
                status_code=503, detail="Catalog credential backend is unavailable"
            ) from exc
    if not await catalog_store.delete_source(workspace_id, source_id):
        raise HTTPException(status_code=404, detail="Catalog source not found")
    logger.info(
        "catalog_source_deleted",
        workspace_id=workspace_id,
        source_id=source_id,
    )
