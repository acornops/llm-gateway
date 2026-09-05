"""Strict matching for the two deterministic MCP secret-name families."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from app.mcp.identity import canonical_mcp_server_id

McpSecretOwnerType = Literal["installation", "user"]


def matches_generated_catalog_secret_name(secret_name: str) -> bool:
    """Match only gateway-generated catalog secret identities."""

    source_prefix = "catalog_source::"
    if secret_name.startswith(source_prefix):
        candidate = secret_name[len(source_prefix) :]
        try:
            return str(UUID(candidate)) == candidate.lower()
        except (TypeError, ValueError, AttributeError):
            return False
    bootstrap_prefix = "catalog_bootstrap::"
    if secret_name.startswith(bootstrap_prefix):
        candidate = secret_name[len(bootstrap_prefix) :]
        return len(candidate) == 24 and all(
            character in "0123456789abcdef" for character in candidate
        )
    return False


def matches_mcp_secret_name(
    secret_name: str,
    workspace_id: str,
    *,
    server_id: str | None = None,
    owner_type: McpSecretOwnerType | None = None,
    owner_id: str | None = None,
) -> bool:
    """Match one MCP secret without exposing a generic prefix-delete API."""

    if owner_id is not None and owner_type != "user":
        raise ValueError("MCP secret owner_id requires owner_type=user")
    generic_prefix = f"mcp_credential::{workspace_id}::"
    oauth_prefix = f"mcp_oauth_tokens::{workspace_id}::"
    if secret_name.startswith(generic_prefix):
        remainder = secret_name[len(generic_prefix) :]
        family = "generic"
    elif secret_name.startswith(oauth_prefix):
        remainder = secret_name[len(oauth_prefix) :]
        family = "oauth"
    else:
        return False

    embedded_server_id, separator, ownership = remainder.partition("::")
    if not separator or not embedded_server_id:
        return False
    if server_id is not None and canonical_mcp_server_id(
        embedded_server_id
    ) != canonical_mcp_server_id(server_id):
        return False

    if family == "oauth":
        if not ownership.startswith("user::"):
            return False
        embedded_owner_type: McpSecretOwnerType = "user"
        embedded_owner_id = ownership[len("user::") :]
    elif ownership == "installation":
        embedded_owner_type = "installation"
        embedded_owner_id = None
    elif ownership.startswith("user::"):
        embedded_owner_type = "user"
        embedded_owner_id = ownership[len("user::") :]
    else:
        return False

    if owner_type is not None and embedded_owner_type != owner_type:
        return False
    if owner_id is not None and embedded_owner_id != owner_id:
        return False
    return bool(embedded_owner_id) if embedded_owner_type == "user" else True
