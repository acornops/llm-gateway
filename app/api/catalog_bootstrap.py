from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError

from app.api.mcp_lifecycle_guard import (
    assert_guarded_workspace_active,
    guarded_workspace_mutation,
)
from app.catalog.schemas import CatalogSourceCreateRequest
from app.catalog.store import catalog_store
from app.config.settings import settings
from app.secrets.errors import SecretNotFoundError
from app.secrets.mcp_names import matches_generated_catalog_secret_name
from app.secrets.store import secret_store

logger = structlog.get_logger()


def _bootstrap_secret_name(workspace_id: str, display_name: str, credential: str) -> str:
    suffix = hashlib.sha256(f"{workspace_id}:{display_name}:{credential}".encode()).hexdigest()[:24]
    return f"catalog_bootstrap::{suffix}"


def _configured_items() -> list[dict[str, Any]]:
    try:
        configured = json.loads(settings.CATALOG_BOOTSTRAP_SOURCES_JSON)
    except json.JSONDecodeError:
        logger.error(
            "catalog_bootstrap_configuration_invalid",
            error_code="CATALOG_BOOTSTRAP_JSON_INVALID",
        )
        return []
    if not isinstance(configured, list):
        logger.error(
            "catalog_bootstrap_configuration_invalid",
            error_code="CATALOG_BOOTSTRAP_JSON_NOT_ARRAY",
        )
        return []
    return [item for item in configured if isinstance(item, dict)]


def _desired_items(workspace_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if settings.CATALOG_OFFICIAL_REGISTRY_ENABLED:
        items.append(
            {
                "workspaceId": "*",
                "displayName": "Official MCP Registry",
                "baseUrl": settings.CATALOG_OFFICIAL_REGISTRY_URL,
                "enabled": True,
                "networkRoute": "direct",
                "auth": {"type": "none"},
            }
        )
    items.extend(_configured_items())
    return [item for item in items if item.get("workspaceId", "*") in {"*", workspace_id}]


def _source_headers(
    auth_type: str, header_name: str | None, credential: str | None
) -> dict[str, str]:
    if auth_type == "none" or credential is None:
        return {}
    effective_name = "Authorization" if auth_type == "bearer_token" else header_name
    if not effective_name:
        return {}
    return {effective_name: (f"Bearer {credential}" if auth_type == "bearer_token" else credential)}


async def _persist_desired_source(
    workspace_id: str,
    validated: CatalogSourceCreateRequest,
    credential: str | None,
    secret_name: str | None,
) -> tuple[object, object, bool, bool] | None:
    """Commit bootstrap secret and source state under short workspace authority."""

    async with guarded_workspace_mutation(workspace_id):
        # The caller's catalog snapshot may predate another replica's create.
        # Re-read under the cross-process workspace guard before deciding
        # create versus update so a losing create cannot delete the winner's
        # deterministic bootstrap credential.
        current_sources = await catalog_store.list_sources(workspace_id)
        existing_pair = next(
            (
                pair
                for pair in current_sources
                if pair[0].display_name == validated.display_name
                and pair[0].management_mode == "bootstrap"
            ),
            None,
        )
        if existing_pair is None and any(
            pair[0].display_name == validated.display_name for pair in current_sources
        ):
            # Display names are workspace-unique. A bootstrap source must never
            # take ownership of a user-managed source merely because the
            # configured display name collides.
            return None
        created = False
        configuration_changed = False
        if existing_pair:
            source = existing_pair[0]
            was_transitioning = bool(
                getattr(source, "credential_transitioning", False)
            )
            previous_secret_name = getattr(source, "previous_auth_secret_name", None)
            old_secret_name = source.auth_secret_name
            secret_changed = old_secret_name != secret_name
            transition_required = was_transitioning or secret_changed
            # A previously staged generated secret may be superseded by new
            # deployment configuration. Delete that abandoned current slot
            # while the row remains fenced, retaining the older rollback cursor.
            if (
                was_transitioning
                and secret_changed
                and old_secret_name
                and old_secret_name != previous_secret_name
                and matches_generated_catalog_secret_name(old_secret_name)
            ):
                with suppress(SecretNotFoundError):
                    await secret_store.delete_secret(
                        old_secret_name,
                        {"workspace_id": workspace_id},
                    )
            if (
                previous_secret_name is None
                and secret_changed
                and old_secret_name
                and matches_generated_catalog_secret_name(old_secret_name)
            ):
                previous_secret_name = old_secret_name
            configuration_changed = any(
                (
                    source.base_url != validated.base_url,
                    source.auth_type != validated.auth_type,
                    old_secret_name != secret_name,
                    source.auth_header_name != validated.auth_header_name,
                    source.network_route != "direct",
                )
            )
            # Persist the exact next-secret and cleanup cursor before the
            # external write. A crash can leave a fenced row, never an
            # unreferenced bootstrap credential.
            updated = await catalog_store.update_source(
                workspace_id,
                str(source.id),
                {
                    "base_url": validated.base_url,
                    "auth_type": validated.auth_type,
                    "auth_secret_name": secret_name,
                    "auth_header_name": validated.auth_header_name,
                    "network_route": "direct",
                    "enabled": validated.enabled,
                    "credential_transitioning": transition_required,
                    "previous_auth_secret_name": previous_secret_name,
                },
                clear_artifacts=configuration_changed,
            )
            if updated is None:
                return None
            source, binding = updated
            if credential and secret_name:
                await secret_store.put_secret(
                    secret_name, credential, {"workspace_id": workspace_id}
                )
            previous_secret_name = getattr(source, "previous_auth_secret_name", None)
            if previous_secret_name:
                with suppress(SecretNotFoundError):
                    await secret_store.delete_secret(
                        previous_secret_name, {"workspace_id": workspace_id}
                    )
            if transition_required:
                finalized = await catalog_store.update_source(
                    workspace_id,
                    str(source.id),
                    {
                        "credential_transitioning": False,
                        "previous_auth_secret_name": None,
                    },
                )
                if finalized is None:
                    return None
                source, binding = finalized
        else:
            created_secret = bool(credential and secret_name)
            try:
                source, binding = await catalog_store.create_source(
                    workspace_id=workspace_id,
                    display_name=validated.display_name,
                    base_url=validated.base_url,
                    auth_type=validated.auth_type,
                    auth_secret_name=secret_name,
                    auth_header_name=validated.auth_header_name,
                    network_route="direct",
                    enabled=validated.enabled,
                    management_mode="bootstrap",
                    artifact_kind="mcp_server",
                    adapter_type="mcp_registry_v0_1",
                    adapter_base_path="/v0.1",
                    credential_transitioning=created_secret,
                )
                created = True
            except IntegrityError:
                return None
            if created_secret and credential and secret_name:
                await secret_store.put_secret(
                    secret_name, credential, {"workspace_id": workspace_id}
                )
                finalized = await catalog_store.update_source(
                    workspace_id,
                    str(source.id),
                    {"credential_transitioning": False},
                )
                if finalized is None:
                    return None
                source, binding = finalized
        return source, binding, created, configuration_changed


async def _reconcile_desired_source(
    workspace_id: str,
    item: dict[str, Any],
) -> str | None:
    display_name = item.get("displayName")
    base_url = item.get("baseUrl")
    if not isinstance(display_name, str) or not isinstance(base_url, str):
        return None
    if item.get("networkRoute", "direct") != "direct":
        logger.warning(
            "catalog_bootstrap_connector_rejected",
            workspace_id=workspace_id,
            display_name=display_name,
            error_code="CATALOG_CONNECTOR_UNAVAILABLE",
        )
        return None
    auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
    auth_type = auth.get("type", "none")
    if auth_type not in {"none", "bearer_token", "custom_header"}:
        return None
    header_name = auth.get("headerName")
    if not isinstance(header_name, str):
        header_name = None
    credential_env = auth.get("credentialEnv")
    credential = (
        os.getenv(credential_env) if isinstance(credential_env, str) and credential_env else None
    )
    enabled = item.get("enabled") is not False
    if auth_type != "none" and not credential:
        enabled = False
        logger.warning(
            "catalog_bootstrap_credential_missing",
            workspace_id=workspace_id,
            display_name=display_name,
            error_code="CATALOG_BOOTSTRAP_CREDENTIAL_MISSING",
        )
    secret_name = (
        _bootstrap_secret_name(workspace_id, display_name, credential) if credential else None
    )
    try:
        validated = CatalogSourceCreateRequest.model_validate(
            {
                "workspace_id": workspace_id,
                "display_name": display_name,
                "base_url": base_url,
                "auth_type": auth_type,
                "auth_secret_name": secret_name,
                "auth_header_name": header_name,
                "network_route": "direct",
                "enabled": enabled,
                "management_mode": "bootstrap",
                "adapter_base_path": "/v0.1",
            }
        )
    except ValueError:
        logger.warning(
            "catalog_bootstrap_source_rejected",
            workspace_id=workspace_id,
            display_name=display_name,
            error_code="CATALOG_BOOTSTRAP_SOURCE_INVALID",
        )
        return None

    persisted = await _persist_desired_source(
        workspace_id,
        validated,
        credential,
        secret_name,
    )
    if persisted is None:
        return validated.display_name
    source, binding, created, configuration_changed = persisted

    should_sync = bool(
        source.enabled and (created or configuration_changed or binding.sync_status == "pending")
    )
    if should_sync:
        try:
            await assert_guarded_workspace_active(workspace_id)
            await catalog_store.sync_mcp_binding(
                source,
                binding,
                headers=_source_headers(auth_type, header_name, credential),
                incremental=False,
            )
        except Exception:
            logger.warning(
                "catalog_bootstrap_sync_failed",
                workspace_id=workspace_id,
                source_id=str(source.id),
                error_code="CATALOG_BOOTSTRAP_SYNC_FAILED",
            )
    return validated.display_name


async def ensure_configured_sources(workspace_id: str) -> None:
    await assert_guarded_workspace_active(workspace_id)
    existing = await catalog_store.list_sources(workspace_id)
    existing_bootstrap = {
        source.display_name: (source, bindings)
        for source, bindings in existing
        if source.management_mode == "bootstrap"
    }
    desired_names: set[str] = set()
    for item in _desired_items(workspace_id):
        desired_name = await _reconcile_desired_source(workspace_id, item)
        if desired_name:
            desired_names.add(desired_name)
    for display_name, (source, _bindings) in existing_bootstrap.items():
        if display_name in desired_names or not source.enabled:
            continue
        async with guarded_workspace_mutation(workspace_id):
            await catalog_store.update_source(
                workspace_id,
                str(source.id),
                {"enabled": False},
            )
        logger.info(
            "catalog_bootstrap_source_disabled",
            workspace_id=workspace_id,
            source_id=str(source.id),
        )
