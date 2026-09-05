"""Canonicalize MCP tool endpoints under their owning server.

Revision ID: b20058629f4b
Revises: a10047518e6a

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b20058629f4b"
down_revision: str | Sequence[str] | None = "a10047518e6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill copied URLs and enforce the server/tool endpoint invariant."""
    active_connection_mismatches = op.get_bind().execute(
        sa.text(
            """
            SELECT COUNT(*)
              FROM gateway_tools AS tools
              JOIN gateway_mcp_servers AS servers
                ON servers.id = tools.server_id
              JOIN gateway_mcp_connections AS connections
                ON connections.server_id = servers.id
               AND connections.workspace_id = servers.workspace_id
             WHERE tools.mcp_server_url IS DISTINCT FROM servers.server_url
            """
        )
    ).scalar_one()
    if active_connection_mismatches:
        raise RuntimeError(
            "MCP endpoint migration blocked: mismatched installations still have "
            "credential connections; revoke and reauthorize them before retrying"
        )
    nonbuiltin_mismatches = op.get_bind().execute(
        sa.text(
            """
            SELECT COUNT(DISTINCT servers.id)
              FROM gateway_tools AS tools
              JOIN gateway_mcp_servers AS servers
                ON servers.id = tools.server_id
             WHERE tools.mcp_server_url IS DISTINCT FROM servers.server_url
               AND servers.provenance_type <> 'builtin'
            """
        )
    ).scalar_one()
    if nonbuiltin_mismatches:
        raise RuntimeError(
            "MCP endpoint migration blocked: mismatched non-built-in installations "
            "still have stale tool definitions; complete endpoint mismatch cleanup "
            "before retrying"
        )
    op.execute(
        sa.text(
            """
            UPDATE gateway_tools AS tools
               SET mcp_server_url = servers.server_url
              FROM gateway_mcp_servers AS servers
             WHERE tools.server_id = servers.id
               AND tools.mcp_server_url IS DISTINCT FROM servers.server_url
            """
        )
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_servers_id_server_url",
        "gateway_mcp_servers",
        ["id", "server_url"],
    )
    op.drop_constraint(
        "gateway_tools_server_id_fkey",
        "gateway_tools",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_gateway_tools_server_endpoint",
        "gateway_tools",
        "gateway_mcp_servers",
        ["server_id", "mcp_server_url"],
        ["id", "server_url"],
        onupdate="CASCADE",
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Restore the legacy server-only foreign key."""
    op.drop_constraint(
        "fk_gateway_tools_server_endpoint",
        "gateway_tools",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "gateway_tools_server_id_fkey",
        "gateway_tools",
        "gateway_mcp_servers",
        ["server_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_gateway_mcp_servers_id_server_url",
        "gateway_mcp_servers",
        type_="unique",
    )
