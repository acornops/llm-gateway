"""Binding between encrypted OAuth tokens and public connection metadata."""

from __future__ import annotations

import hashlib
import json

from app.mcp.oauth.models import OAuthEndpointSnapshot


def token_binding_fingerprint(
    *,
    endpoint_snapshot: OAuthEndpointSnapshot,
    client_id: str,
    resource: str,
) -> str:
    canonical = json.dumps(
        {
            "client_id": client_id,
            "resource": resource,
            "endpoint_snapshot": endpoint_snapshot.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def connection_token_binding(
    connection,
) -> tuple[str, str, OAuthEndpointSnapshot, str] | None:
    client_id = getattr(connection, "oauth_client_id", None)
    resource = getattr(connection, "oauth_resource", None)
    raw_endpoint_snapshot = getattr(connection, "oauth_endpoint_snapshot", None)
    if (
        not isinstance(client_id, str)
        or not client_id
        or not isinstance(resource, str)
        or not resource
        or not isinstance(raw_endpoint_snapshot, dict)
    ):
        return None
    try:
        endpoint_snapshot = OAuthEndpointSnapshot.model_validate(
            raw_endpoint_snapshot
        )
    except Exception:
        return None
    return (
        client_id,
        resource,
        endpoint_snapshot,
        token_binding_fingerprint(
            endpoint_snapshot=endpoint_snapshot,
            client_id=client_id,
            resource=resource,
        ),
    )
