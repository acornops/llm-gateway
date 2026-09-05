"""Pure response, snapshot, and secret-identity helpers for catalog sources."""

from __future__ import annotations

import hmac
import uuid
from typing import Any

from fastapi import HTTPException

from app.catalog.models import CatalogBinding, CatalogSource
from app.catalog.schemas import (
    CatalogBindingResponse,
    CatalogSourcePatchRequest,
    CatalogSourceResponse,
)
from app.secrets.errors import SecretNotFoundError


def catalog_source_create_secret_name(source_id: uuid.UUID) -> str:
    """Bind a generated credential identity to immutable source identity."""

    return f"catalog_source::{source_id}"


def catalog_source_replacement_secret_name(
    source_id: object, current_secret_name: str | None
) -> str:
    slot_a_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"acornops:catalog-source:{source_id}:a",
    )
    slot_b_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"acornops:catalog-source:{source_id}:b",
    )
    slot_a = f"catalog_source::{slot_a_id}"
    slot_b = f"catalog_source::{slot_b_id}"
    return slot_b if current_secret_name == slot_a else slot_a


def catalog_source_owned_secret_names(source_id: object) -> set[str]:
    """Return every bounded credential slot owned by one immutable source."""

    initial = catalog_source_create_secret_name(uuid.UUID(str(source_id)))
    slot_a = catalog_source_replacement_secret_name(source_id, None)
    slot_b = catalog_source_replacement_secret_name(source_id, slot_a)
    return {initial, slot_a, slot_b}


def source_mutation_snapshot(
    source: CatalogSource,
    binding: CatalogBinding,
) -> tuple[object, ...]:
    """Capture fields that authorize and derive one optimistic source PATCH."""

    return (
        str(source.id),
        int(getattr(source, "authority_generation", 1) or 1),
        source.display_name,
        source.base_url,
        source.auth_type,
        source.auth_secret_name,
        source.auth_header_name,
        source.network_route,
        bool(source.enabled),
        source.management_mode,
        bool(getattr(source, "credential_transitioning", False)),
        getattr(source, "previous_auth_secret_name", None),
        str(binding.id),
        binding.adapter_base_path,
    )


async def patch_matches_persisted_source(
    request: CatalogSourcePatchRequest,
    source: CatalogSource,
    workspace_id: str,
    *,
    secret_store: Any,
) -> bool:
    """Prove a retry describes the transition already persisted on the source."""

    scalar_fields = (
        ("display_name", source.display_name),
        ("base_url", source.base_url),
        ("enabled", bool(source.enabled)),
        ("network_route", source.network_route),
    )
    for field_name, persisted in scalar_fields:
        if field_name in request.model_fields_set and getattr(request, field_name) != persisted:
            return False
    if "auth" not in request.model_fields_set:
        return True
    assert request.auth is not None
    if request.auth.type != source.auth_type:
        return False
    expected_header = request.auth.header_name if request.auth.type == "custom_header" else None
    if expected_header != source.auth_header_name:
        return False
    if request.auth.type == "none":
        return source.auth_secret_name is None
    if source.auth_secret_name is None or request.auth.credential is None:
        return False
    try:
        persisted_credential = await secret_store.get_secret(
            source.auth_secret_name,
            {"workspace_id": workspace_id},
        )
    except SecretNotFoundError:
        return False
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Catalog credential backend is unavailable"
        ) from exc
    return hmac.compare_digest(
        persisted_credential.encode("utf-8"),
        request.auth.credential.encode("utf-8"),
    )


def binding_response(binding: CatalogBinding) -> CatalogBindingResponse:
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


def source_response(
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
        bindings=[binding_response(binding) for binding in bindings],
        created_at=source.created_at,
        updated_at=source.updated_at,
    )
