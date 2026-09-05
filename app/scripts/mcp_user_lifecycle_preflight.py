"""Report the one-time individual MCP credential reset and rollout invariants."""

import argparse
import asyncio
import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.mcp_connection_cleanup import cleanup_user_server_connection
from app.config.settings import settings
from app.mcp.oauth.flow_store import oauth_flow_store
from app.outbound_tls import sqlalchemy_connection_config
from app.secrets.store import secret_store


@dataclass(frozen=True)
class UserLifecyclePreflight:
    individual_connection_count: int
    individual_owner_count: int
    workspace_connection_count: int
    unbound_individual_connection_count: int
    mcp_server_count: int
    individual_secret_object_count: int = 0
    workspace_secret_object_count: int = 0
    oauth_flow_record_count: int = 0

    @property
    def known_owner_server_cleanup_upper_bound(self) -> int:
        return self.individual_owner_count * self.mcp_server_count


async def _load_preflight() -> UserLifecyclePreflight:
    database_url, connect_args = sqlalchemy_connection_config(settings.DATABASE_URL)
    engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT COUNT(*) FILTER (
                                   WHERE connections.owner_type = 'user'
                               ) AS individual_connection_count,
                               COUNT(DISTINCT (
                                   connections.workspace_id,
                                   connections.owner_id
                               )) FILTER (
                                   WHERE connections.owner_type = 'user'
                               ) AS individual_owner_count,
                               COUNT(*) FILTER (
                                   WHERE connections.owner_type = 'installation'
                               ) AS workspace_connection_count,
                               COUNT(*) FILTER (
                                   WHERE connections.owner_type = 'user'
                                     AND (
                                         lifecycle.status IS DISTINCT FROM 'active'
                                         OR lifecycle.membership_generation
                                            IS DISTINCT FROM connections.membership_generation
                                     )
                               ) AS unbound_individual_connection_count,
                               (
                                   SELECT COUNT(*) FROM gateway_mcp_servers
                               ) AS mcp_server_count
                          FROM gateway_mcp_connections AS connections
                          LEFT JOIN gateway_mcp_user_lifecycles AS lifecycle
                            ON lifecycle.workspace_id = connections.workspace_id
                           AND lifecycle.user_id = connections.owner_id
                        """
                    )
                )
            ).mappings().one()
            return UserLifecyclePreflight(
                individual_connection_count=int(row["individual_connection_count"] or 0),
                individual_owner_count=int(row["individual_owner_count"] or 0),
                workspace_connection_count=int(row["workspace_connection_count"] or 0),
                unbound_individual_connection_count=int(
                    row["unbound_individual_connection_count"] or 0
                ),
                mcp_server_count=int(row["mcp_server_count"] or 0),
                individual_secret_object_count=(
                    await secret_store.count_all_mcp_user_secrets()
                ),
                workspace_secret_object_count=(
                    await secret_store.count_all_mcp_installation_secrets()
                ),
                oauth_flow_record_count=(
                    await oauth_flow_store.count_all_flow_records()
                ),
            )
    finally:
        await engine.dispose()


def _report(preflight: UserLifecyclePreflight) -> None:
    print(
        json.dumps(
            {
                "individualConnectionResetCount": preflight.individual_connection_count,
                "individualOwnerResetCount": preflight.individual_owner_count,
                "workspaceOwnedConnectionCountUnaffected": (
                    preflight.workspace_connection_count
                ),
                "unboundIndividualConnectionCount": (
                    preflight.unbound_individual_connection_count
                ),
                "mcpServerCount": preflight.mcp_server_count,
                "individualSecretObjectCount": (
                    preflight.individual_secret_object_count
                ),
                "workspaceOwnedSecretObjectCountUnaffected": (
                    preflight.workspace_secret_object_count
                ),
                "oauthFlowRecordCount": preflight.oauth_flow_record_count,
                "knownOwnerServerCleanupUpperBound": (
                    preflight.known_owner_server_cleanup_upper_bound
                ),
            },
            sort_keys=True,
        )
    )


async def _load_user_connection_reset_rows() -> list[tuple[str, str, str]]:
    database_url, connect_args = sqlalchemy_connection_config(settings.DATABASE_URL)
    engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            return [
                (str(workspace_id), str(server_id), str(user_id))
                for workspace_id, server_id, user_id in (
                    await connection.execute(
                        text(
                            """
                            SELECT workspace_id, server_id::text, owner_id
                              FROM gateway_mcp_connections
                             WHERE owner_type = 'user'
                             ORDER BY workspace_id, server_id, owner_id
                            """
                        )
                    )
                ).all()
            ]
    finally:
        await engine.dispose()


async def _run(
    *,
    fail_on_unbound: bool,
    apply_individual_reset: bool = False,
) -> int:
    preflight = await _load_preflight()
    if apply_individual_reset:
        baseline_workspace_connections = preflight.workspace_connection_count
        baseline_workspace_secrets = preflight.workspace_secret_object_count
        for workspace_id, server_id, user_id in await _load_user_connection_reset_rows():
            await cleanup_user_server_connection(
                workspace_id,
                server_id,
                user_id,
                reason="rollout_individual_reset",
            )
        await secret_store.purge_all_mcp_user_secrets()
        await oauth_flow_store.purge_all_flow_state()
        preflight = await _load_preflight()
        workspace_baseline_changed = bool(
            preflight.workspace_connection_count != baseline_workspace_connections
            or preflight.workspace_secret_object_count != baseline_workspace_secrets
        )
    else:
        workspace_baseline_changed = False
    _report(preflight)
    if apply_individual_reset and (
        preflight.individual_connection_count
        or preflight.individual_secret_object_count
        or preflight.oauth_flow_record_count
        or workspace_baseline_changed
    ):
        return 2
    if fail_on_unbound and (
        preflight.individual_connection_count
        or preflight.unbound_individual_connection_count
        or preflight.individual_secret_object_count
        or preflight.oauth_flow_record_count
    ):
        return 2
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report or apply the pinned offline reset of individual MCP credentials "
            "and verify that no unbound or rowless individual state remains."
        )
    )
    parser.add_argument(
        "--apply-individual-reset",
        action="store_true",
        help=(
            "Purge exact user-owned MCP secret families and all bounded OAuth flow "
            "state after both pinned services have been stopped."
        ),
    )
    parser.add_argument(
        "--fail-on-unbound",
        action="store_true",
        help=(
            "Exit 2 if any individual connection, user-owned MCP secret, OAuth flow "
            "state, or non-exact lifecycle binding remains."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    raise SystemExit(
        asyncio.run(
            _run(
                fail_on_unbound=args.fail_on_unbound,
                apply_individual_reset=args.apply_individual_reset,
            )
        )
    )


if __name__ == "__main__":
    main()
