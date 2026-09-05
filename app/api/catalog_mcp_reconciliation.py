"""Catalog MCP trust-transition reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.catalog.models import CatalogArtifact
from app.catalog.schemas import CatalogMcpImportBase


async def reimport_catalog_server_locked(
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
    server_registry: Any,
    tool_registry: Any,
    cleanup_server_connections: Any,
    logger: Any,
) -> tuple[object, bool]:
    """Persist one reimport while the canonical server guard is held."""

    if getattr(current, "provenance_type", None) != "catalog":
        raise HTTPException(
            status_code=409,
            detail="Only catalog installations can be explicitly reimported",
        )
    if (
        getattr(current, "catalog_source_id", None) != artifact.source_id
        or getattr(current, "catalog_artifact_name", None) != artifact.artifact_name
    ):
        raise HTTPException(
            status_code=409,
            detail="Catalog reimport provenance does not match this installation",
        )
    endpoint_owner = await server_registry.get_server_by_url(
        request.workspace_id,
        destination_id,
        resolved_endpoint,
        enabled_only=False,
        **registry_scope,
    )
    if endpoint_owner is not None and endpoint_owner.id != current.id:
        raise HTTPException(
            status_code=409,
            detail="Selected endpoint is owned by another installation in this destination",
        )

    trust_changed = any(
        (
            getattr(current, "server_url", None) != resolved_endpoint,
            getattr(current, "auth_type", None)
            != (credential_auth_type if requires_credential else "none"),
            getattr(current, "auth_header_name", None)
            != (credential_header_name if requires_credential else None),
            getattr(current, "auth_header_prefix", None)
            != (credential_auth_header_prefix if requires_credential else None),
            getattr(current, "credential_mode", None) != credential_mode,
            (getattr(current, "public_headers", None) or {}) != (public_headers or {}),
        )
    )
    server_patch: dict[str, object] = {
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
        "connection_status": "unknown",
        "last_discovery_at": None,
        "last_discovery_error": None,
    }
    current_revision = int(current.revision)
    expected_revision = request.expected_revision
    transitioning_state = bool(getattr(current, "credential_transitioning", False))
    persisted_catalog_request_matches = all(
        (
            getattr(current, "server_name", None) == server_name,
            getattr(current, "server_url", None) == resolved_endpoint,
            bool(getattr(current, "enabled", True)) == bool(request.enabled),
            getattr(current, "catalog_source_id", None) == artifact.source_id,
            getattr(current, "catalog_artifact_name", None) == artifact.artifact_name,
            getattr(current, "catalog_version", None) == artifact.version,
            getattr(current, "catalog_digest", None) == artifact.digest,
            (getattr(current, "endpoint_configuration", {}) or {})
            == request.endpoint_configuration,
        )
    )
    discovery_recovery = (
        transitioning_state and not trust_changed and persisted_catalog_request_matches
    )
    cleanup_recovery = (
        transitioning_state
        and trust_changed
        and expected_revision is not None
        and expected_revision == current_revision - 1
    )
    if (
        expected_revision is not None
        and current_revision != expected_revision
        and not discovery_recovery
        and not cleanup_recovery
    ):
        raise HTTPException(status_code=409, detail="MCP server revision does not match")

    if trust_changed:
        transitioning = current
        if not cleanup_recovery:
            transitioning = await server_registry.update_server(
                request.workspace_id,
                destination_id,
                str(current.id),
                {
                    "expected_revision": request.expected_revision,
                    "credential_transitioning": True,
                    "credential_epoch": int(
                        getattr(current, "credential_epoch", 1) or 1
                    )
                    + 1,
                    "connection_status": "error",
                    "last_discovery_at": None,
                    "last_discovery_error": (
                        "Credential configuration update in progress."
                    ),
                },
                **registry_scope,
            )
        if transitioning is None:
            raise HTTPException(status_code=404, detail="MCP installation not found")
        try:
            await cleanup_server_connections(
                request.workspace_id,
                str(current.id),
                reason=(
                    "mode_transition"
                    if getattr(current, "credential_mode", None) != credential_mode
                    else "trust_change"
                ),
            )
        except Exception as exc:
            logger.exception(
                "catalog_mcp_connection_cleanup_failed",
                workspace_id=request.workspace_id,
                server_id=str(current.id),
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Catalog MCP credential cleanup did not complete; retry this reimport"
                ),
            ) from exc
        logger.info(
            "mcp_connections_invalidated_for_trust_change",
            workspace_id=request.workspace_id,
            scope_type=request.scope_type,
            server_id=str(current.id),
        )
        # Reviewed authority is endpoint/auth-context specific. Reconnection
        # must recreate every definition disabled and pending review.
        await tool_registry.remove_server_tools_not_in(
            request.workspace_id,
            destination_id,
            server_id=str(current.id),
            tool_names=set(),
            **registry_scope,
        )
        server_patch["expected_revision"] = int(transitioning.revision)
        if requires_credential:
            server_patch["credential_transitioning"] = False
    else:
        server_patch["expected_revision"] = (
            current_revision if discovery_recovery else request.expected_revision
        )

    credential_free_reconciliation = not requires_credential and (
        trust_changed or transitioning_state
    )
    if credential_free_reconciliation:
        server_patch["credential_transitioning"] = True
    server = await server_registry.update_server(
        request.workspace_id,
        destination_id,
        str(current.id),
        server_patch,
        **registry_scope,
    )
    if server is None:
        raise HTTPException(status_code=404, detail="MCP installation not found")
    return server, credential_free_reconciliation
