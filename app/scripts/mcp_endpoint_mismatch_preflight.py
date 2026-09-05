"""Audit and safely clean credential connections before MCP endpoint canonicalization."""

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config.settings import settings
from app.mcp.connections import ConnectionOwner, credential_secret_name
from app.mcp.oauth.flow_store import oauth_flow_store
from app.mcp.oauth.registration_store import oauth_registration_store
from app.mcp.oauth.tokens import oauth_token_service
from app.outbound_tls import sqlalchemy_connection_config
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store


@dataclass(frozen=True)
class MismatchedInstallation:
    workspace_id: str
    server_id: str
    connection_count: int
    provenance_type: str = "manual"
    credential_transitioning: bool = False


@dataclass(frozen=True)
class LegacyConnection:
    """Only columns present in the a100 pre-lifecycle connection schema."""

    id: str
    workspace_id: str
    server_id: str
    owner_type: str
    owner_id: str
    oauth_issuer: str | None
    oauth_resource: str | None
    oauth_client_id: str | None
    oauth_endpoint_snapshot: dict[str, Any] | None


@dataclass(frozen=True)
class DuplicateBuiltinDestination:
    workspace_id: str
    scope_type: str
    destination_id: str
    target_type: str | None
    server_ids: tuple[str, ...]


Cleanup = Callable[[str, str], Awaitable[int]]


async def _load_duplicate_builtin_destinations(
    *,
    engine: AsyncEngine | None = None,
) -> list[DuplicateBuiltinDestination]:
    owns_engine = engine is None
    if engine is None:
        database_url, connect_args = sqlalchemy_connection_config(settings.DATABASE_URL)
        engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT workspace_id,
                               scope_type,
                               agent_id,
                               target_id,
                               target_type,
                               id
                          FROM gateway_mcp_servers
                         WHERE provenance_type = 'builtin'
                         ORDER BY workspace_id,
                                  scope_type,
                                  agent_id,
                                  target_id,
                                  target_type,
                                  id
                        """
                    )
                )
            ).mappings()
            grouped: dict[tuple[str, str, str, str | None], list[str]] = {}
            for row in rows:
                scope_type = str(row["scope_type"])
                destination_id = row["agent_id"] if scope_type == "agent" else row["target_id"]
                if destination_id is None:
                    continue
                target_type = (
                    None
                    if scope_type == "agent" or row["target_type"] is None
                    else str(row["target_type"])
                )
                key = (
                    str(row["workspace_id"]),
                    scope_type,
                    str(destination_id),
                    target_type,
                )
                grouped.setdefault(key, []).append(str(row["id"]))
            return [
                DuplicateBuiltinDestination(
                    workspace_id=key[0],
                    scope_type=key[1],
                    destination_id=key[2],
                    target_type=key[3],
                    server_ids=tuple(server_ids),
                )
                for key, server_ids in grouped.items()
                if len(server_ids) > 1
            ]
    finally:
        if owns_engine:
            await engine.dispose()


async def _load_mismatched_installations() -> list[MismatchedInstallation]:
    database_url, connect_args = sqlalchemy_connection_config(settings.DATABASE_URL)
    engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        WITH mismatched AS (
                            SELECT DISTINCT servers.workspace_id, servers.id AS server_id
                              FROM gateway_tools AS tools
                              JOIN gateway_mcp_servers AS servers
                                ON servers.id = tools.server_id
                             WHERE tools.mcp_server_url IS DISTINCT FROM servers.server_url
                        )
                        SELECT mismatched.workspace_id,
                               mismatched.server_id,
                               servers.provenance_type,
                               servers.credential_transitioning,
                               COUNT(DISTINCT connections.id) AS connection_count
                          FROM mismatched
                          JOIN gateway_mcp_servers AS servers
                            ON servers.workspace_id = mismatched.workspace_id
                           AND servers.id = mismatched.server_id
                          LEFT JOIN gateway_mcp_connections AS connections
                            ON connections.workspace_id = mismatched.workspace_id
                           AND connections.server_id = mismatched.server_id
                         GROUP BY mismatched.workspace_id,
                                  mismatched.server_id,
                                  servers.provenance_type,
                                  servers.credential_transitioning
                         ORDER BY mismatched.workspace_id, mismatched.server_id
                        """
                    )
                )
            ).mappings()
            return [
                MismatchedInstallation(
                    workspace_id=str(row["workspace_id"]),
                    server_id=str(row["server_id"]),
                    connection_count=int(row["connection_count"]),
                    provenance_type=str(row["provenance_type"]),
                    credential_transitioning=bool(row["credential_transitioning"]),
                )
                for row in rows
            ]
    finally:
        await engine.dispose()


async def _cleanup_connections(
    installations: list[MismatchedInstallation],
    cleanup: Cleanup,
) -> int:
    cleaned = 0
    for installation in installations:
        cleaned += await cleanup(installation.workspace_id, installation.server_id)
    return cleaned


async def _cleanup_legacy_installation(
    workspace_id: str,
    server_id: str,
    *,
    engine: AsyncEngine | None = None,
) -> int:
    """Clean one installation without selecting c300-only ORM columns.

    This maintenance command intentionally runs against the a100 schema before
    b200 can enforce the endpoint foreign key. The old gateway/control-plane
    pair must already be stopped, so the explicit legacy-column query and final
    row delete cannot race a credential writer.
    """

    owns_engine = engine is None
    if engine is None:
        database_url, connect_args = sqlalchemy_connection_config(settings.DATABASE_URL)
        engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE gateway_mcp_servers
                       SET credential_transitioning = TRUE,
                           revision = revision + 1,
                           connection_status = 'error',
                           last_discovery_at = NULL,
                           last_discovery_error =
                               'Endpoint canonicalization requires authoritative rediscovery.'
                     WHERE workspace_id = :workspace_id
                       AND id = :server_id
                       AND provenance_type <> 'builtin'
                       AND credential_transitioning IS NOT TRUE
                    """
                ),
                {"workspace_id": workspace_id, "server_id": server_id},
            )
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id,
                               workspace_id,
                               server_id,
                               owner_type,
                               owner_id,
                               oauth_issuer,
                               oauth_resource,
                               oauth_client_id,
                               oauth_endpoint_snapshot
                          FROM gateway_mcp_connections
                         WHERE workspace_id = :workspace_id
                           AND server_id = :server_id
                         ORDER BY id
                        """
                    ),
                    {"workspace_id": workspace_id, "server_id": server_id},
                )
            ).mappings()
            connections = [LegacyConnection(**dict(row)) for row in rows]

        cleaned = 0
        for connection in connections:
            if connection.oauth_issuer:
                await oauth_token_service.revoke(
                    workspace_id=workspace_id,
                    server_id=server_id,
                    owner_id=connection.owner_id,
                    connection=connection,
                )
            owner = ConnectionOwner(
                connection.owner_type,  # type: ignore[arg-type]
                connection.owner_id,
            )
            # Pre-c300 history can contain an opposite-type orphan after an auth
            # transition. Remove both deterministic identities before the raw
            # legacy row, just like the runtime terminal cleanup boundary.
            with suppress(SecretNotFoundError):
                await secret_store.delete_secret(
                    credential_secret_name(workspace_id, server_id, owner),
                    {"workspace_id": workspace_id},
                )
            await oauth_token_service.delete_tokens(
                workspace_id,
                server_id,
                connection.owner_id,
            )
            await oauth_flow_store.delete_for_connection(
                workspace_id,
                server_id,
                connection.owner_id,
            )
            async with engine.begin() as database_connection:
                result = await database_connection.execute(
                    text(
                        """
                        DELETE FROM gateway_mcp_connections
                         WHERE id = :connection_id
                           AND workspace_id = :workspace_id
                           AND server_id = :server_id
                        """
                    ),
                    {
                        "connection_id": connection.id,
                        "workspace_id": workspace_id,
                        "server_id": server_id,
                    },
                )
            cleaned += int(result.rowcount or 0)

        await oauth_flow_store.delete_for_server(workspace_id, server_id)
        await oauth_registration_store.delete_for_server(workspace_id, server_id)
        await secret_store.purge_mcp_secrets(workspace_id, server_id=server_id)
        if await secret_store.count_mcp_secrets(
            workspace_id,
            server_id=server_id,
        ):
            raise RuntimeError("Endpoint cleanup left MCP secret objects")
        # Keep the mismatched tool rows as the retry cursor until every external
        # cleanup succeeds. Only then remove authority reviewed at the copied
        # legacy URL; discovery recreates definitions disabled/pending review.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM gateway_tools
                     WHERE workspace_id = :workspace_id
                       AND server_id = :server_id
                       AND EXISTS (
                           SELECT 1
                             FROM gateway_mcp_servers
                            WHERE gateway_mcp_servers.workspace_id = :workspace_id
                              AND gateway_mcp_servers.id = :server_id
                              AND gateway_mcp_servers.provenance_type <> 'builtin'
                       )
                    """
                ),
                {"workspace_id": workspace_id, "server_id": server_id},
            )
        return cleaned
    finally:
        if owns_engine:
            await engine.dispose()


async def _cleanup_with_reason(workspace_id: str, server_id: str) -> int:
    return await _cleanup_legacy_installation(workspace_id, server_id)


async def _dedupe_builtin_destinations(
    duplicates: list[DuplicateBuiltinDestination],
    canonical_server_ids: list[str],
    *,
    engine: AsyncEngine | None = None,
) -> int:
    """Delete noncanonical duplicates selected explicitly by an operator."""

    if not canonical_server_ids:
        return 0
    by_server_id = {
        server_id: duplicate
        for duplicate in duplicates
        for server_id in duplicate.server_ids
    }
    selected: dict[tuple[str, str, str, str | None], str] = {}
    for server_id in canonical_server_ids:
        duplicate = by_server_id.get(server_id)
        if duplicate is None:
            raise ValueError(
                f"Canonical built-in server {server_id} is not in a duplicate destination"
            )
        key = (
            duplicate.workspace_id,
            duplicate.scope_type,
            duplicate.destination_id,
            duplicate.target_type,
        )
        previous = selected.setdefault(key, server_id)
        if previous != server_id:
            raise ValueError("Exactly one canonical server may be selected per destination")

    owns_engine = engine is None
    if engine is None:
        database_url, connect_args = sqlalchemy_connection_config(settings.DATABASE_URL)
        engine = create_async_engine(database_url, connect_args=connect_args)
    deleted = 0
    try:
        for duplicate in duplicates:
            key = (
                duplicate.workspace_id,
                duplicate.scope_type,
                duplicate.destination_id,
                duplicate.target_type,
            )
            canonical_id = selected.get(key)
            if canonical_id is None:
                continue
            for server_id in duplicate.server_ids:
                if server_id == canonical_id:
                    continue
                await _cleanup_legacy_installation(
                    duplicate.workspace_id,
                    server_id,
                    engine=engine,
                )
                async with engine.begin() as connection:
                    result = await connection.execute(
                        text(
                            """
                            DELETE FROM gateway_mcp_servers
                             WHERE workspace_id = :workspace_id
                               AND id = :server_id
                               AND provenance_type = 'builtin'
                            """
                        ),
                        {
                            "workspace_id": duplicate.workspace_id,
                            "server_id": server_id,
                        },
                    )
                deleted += int(result.rowcount or 0)
        return deleted
    finally:
        if owns_engine:
            await engine.dispose()


def _report(
    installations: list[MismatchedInstallation],
    duplicate_builtins: list[DuplicateBuiltinDestination],
    *,
    cleanup_applied: bool,
    cleaned_connection_count: int = 0,
    deleted_duplicate_builtin_count: int = 0,
) -> None:
    print(
        json.dumps(
            {
                "mismatchedInstallationCount": len(installations),
                "credentialConnectionCount": sum(
                    installation.connection_count for installation in installations
                ),
                "unfencedNonBuiltinInstallationCount": sum(
                    installation.provenance_type != "builtin"
                    and not installation.credential_transitioning
                    for installation in installations
                ),
                "cleanupApplied": cleanup_applied,
                "cleanedConnectionCount": cleaned_connection_count,
                "duplicateBuiltinDestinationCount": len(duplicate_builtins),
                "deletedDuplicateBuiltinServerCount": deleted_duplicate_builtin_count,
                "installations": [
                    {
                        "workspaceId": installation.workspace_id,
                        "serverId": installation.server_id,
                        "connectionCount": installation.connection_count,
                        "provenanceType": installation.provenance_type,
                        "credentialTransitioning": installation.credential_transitioning,
                    }
                    for installation in installations
                ],
                "duplicateBuiltinDestinations": [
                    {
                        "workspaceId": duplicate.workspace_id,
                        "scopeType": duplicate.scope_type,
                        "destinationId": duplicate.destination_id,
                        "targetType": duplicate.target_type,
                        "serverIds": list(duplicate.server_ids),
                    }
                    for duplicate in duplicate_builtins
                ],
            },
            sort_keys=True,
        )
    )


async def _run(
    *,
    apply_cleanup: bool,
    fail_on_active_connections: bool,
    fail_on_duplicate_builtins: bool,
    canonical_builtin_server_ids: list[str],
) -> int:
    installations = await _load_mismatched_installations()
    duplicate_builtins = await _load_duplicate_builtin_destinations()
    active_connection_count = sum(
        installation.connection_count for installation in installations
    )
    nonbuiltin_mismatch = any(
        installation.provenance_type != "builtin" for installation in installations
    )
    if not apply_cleanup:
        if canonical_builtin_server_ids:
            raise ValueError("Canonical built-in IDs require --apply-cleanup")
        _report(installations, duplicate_builtins, cleanup_applied=False)
        return (
            2
            if (
                fail_on_active_connections
                and (active_connection_count or nonbuiltin_mismatch)
            )
            or (fail_on_duplicate_builtins and duplicate_builtins)
            else 0
        )

    cleaned = await _cleanup_connections(installations, _cleanup_with_reason)
    deleted_duplicates = await _dedupe_builtin_destinations(
        duplicate_builtins,
        canonical_builtin_server_ids,
    )
    remaining = await _load_mismatched_installations()
    remaining_duplicates = await _load_duplicate_builtin_destinations()
    _report(
        remaining,
        remaining_duplicates,
        cleanup_applied=True,
        cleaned_connection_count=cleaned,
        deleted_duplicate_builtin_count=deleted_duplicates,
    )
    remaining_connections = sum(installation.connection_count for installation in remaining)
    remaining_nonbuiltin = any(
        installation.provenance_type != "builtin"
        for installation in remaining
    )
    return (
        2
        if remaining_connections
        or remaining_nonbuiltin
        or remaining_duplicates
        else 0
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit MCP tool/server endpoint mismatches and optionally revoke every "
            "credential connection and durably fence non-built-in installations "
            "until authoritative rediscovery."
        )
    )
    parser.add_argument(
        "--apply-cleanup",
        action="store_true",
        help=(
            "Fence affected non-built-in servers and revoke OAuth tokens and "
            "secret-backed connections."
        ),
    )
    parser.add_argument(
        "--fail-on-active-connections",
        action="store_true",
        help=(
            "Exit 2 when the report still contains credential connections or any "
            "non-built-in endpoint mismatch."
        ),
    )
    parser.add_argument(
        "--fail-on-duplicate-builtins",
        action="store_true",
        help="Exit 2 when a destination still contains duplicate built-in servers.",
    )
    parser.add_argument(
        "--canonical-builtin-server-id",
        action="append",
        default=[],
        help=(
            "With --apply-cleanup, preserve this operator-verified built-in server "
            "ID and clean/delete its duplicate siblings. Repeat per destination."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    raise SystemExit(
        asyncio.run(
            _run(
                apply_cleanup=args.apply_cleanup,
                fail_on_active_connections=args.fail_on_active_connections,
                fail_on_duplicate_builtins=args.fail_on_duplicate_builtins,
                canonical_builtin_server_ids=args.canonical_builtin_server_id,
            )
        )
    )


if __name__ == "__main__":
    main()
