"""Add generic catalog registry, provenance, and server-qualified MCP tools.

Revision ID: d4a91c2e7b30
Revises: c84d9e2a61f0
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4a91c2e7b30"
down_revision = "c84d9e2a61f0"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "gateway_catalog_sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("auth_type", sa.String(), server_default="none", nullable=False),
        sa.Column("auth_secret_name", sa.String(), nullable=True),
        sa.Column("auth_header_name", sa.String(), nullable=True),
        sa.Column("network_route", sa.String(), server_default="direct", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("management_mode", sa.String(), server_default="workspace", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "display_name", name="uq_gateway_catalog_source_name"
        ),
        sa.CheckConstraint(
            "auth_type IN ('none', 'bearer_token', 'custom_header')",
            name="ck_gateway_catalog_source_auth_type",
        ),
        sa.CheckConstraint(
            "network_route IN ('direct', 'connector')",
            name="ck_gateway_catalog_source_network_route",
        ),
        sa.CheckConstraint(
            "management_mode IN ('workspace', 'bootstrap')",
            name="ck_gateway_catalog_source_management_mode",
        ),
    )
    op.create_index(
        "ix_gateway_catalog_sources_workspace",
        "gateway_catalog_sources",
        ["workspace_id", "enabled"],
    )
    op.create_table(
        "gateway_catalog_bindings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("artifact_kind", sa.String(), nullable=False),
        sa.Column("adapter_type", sa.String(), nullable=False),
        sa.Column("adapter_base_path", sa.String(), server_default="/v0.1", nullable=False),
        sa.Column("sync_status", sa.String(), server_default="pending", nullable=False),
        sa.Column("last_sync_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.String(), nullable=True),
        sa.Column("sync_cursor", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["gateway_catalog_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "artifact_kind", name="uq_gateway_catalog_binding_kind"
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('mcp_server', 'agent_skill')",
            name="ck_gateway_catalog_binding_artifact_kind",
        ),
        sa.CheckConstraint(
            "sync_status IN ('pending', 'syncing', 'ready', 'error')",
            name="ck_gateway_catalog_binding_sync_status",
        ),
    )
    op.create_index(
        "ix_gateway_catalog_bindings_source",
        "gateway_catalog_bindings",
        ["source_id"],
    )
    op.create_table(
        "gateway_catalog_artifacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("binding_id", sa.UUID(), nullable=False),
        sa.Column("artifact_kind", sa.String(), nullable=False),
        sa.Column("artifact_name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("metadata_json", _json_type(), nullable=False),
        sa.Column("payload_json", _json_type(), nullable=False),
        sa.Column("compatible", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("incompatibility_reason", sa.String(), nullable=True),
        sa.Column("remote_endpoints", _json_type(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["gateway_catalog_sources.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"], ["gateway_catalog_bindings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "binding_id",
            "artifact_name",
            "version",
            name="uq_gateway_catalog_artifact_version",
        ),
    )
    op.create_index(
        "ix_gateway_catalog_artifacts_workspace_kind",
        "gateway_catalog_artifacts",
        ["workspace_id", "artifact_kind", "artifact_name"],
    )

    op.add_column("gateway_tools", sa.Column("server_id", sa.UUID(), nullable=True))
    op.execute(
        """
        UPDATE gateway_tools AS tool
        SET server_id = server.id
        FROM gateway_mcp_servers AS server
        WHERE server.workspace_id = tool.workspace_id
          AND server.scope_type = tool.scope_type
          AND server.target_id = tool.target_id
          AND server.target_type = tool.target_type
          AND server.server_url = tool.mcp_server_url
        """
    )
    op.alter_column("gateway_tools", "server_id", nullable=False)
    op.create_foreign_key(
        "fk_gateway_tools_server_id",
        "gateway_tools",
        "gateway_mcp_servers",
        ["server_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_gateway_tools_ws_target_name", "gateway_tools", type_="unique")
    op.create_unique_constraint(
        "uq_gateway_tools_ws_target_name",
        "gateway_tools",
        ["server_id", "tool_name"],
    )

    op.add_column(
        "gateway_mcp_servers",
        sa.Column("auth_scope", sa.String(), server_default="legacy_shared", nullable=False),
    )
    op.add_column(
        "gateway_mcp_servers", sa.Column("catalog_source_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "gateway_mcp_servers", sa.Column("catalog_artifact_name", sa.String(), nullable=True)
    )
    op.add_column(
        "gateway_mcp_servers", sa.Column("catalog_version", sa.String(), nullable=True)
    )
    op.add_column(
        "gateway_mcp_servers", sa.Column("catalog_digest", sa.String(), nullable=True)
    )
    op.add_column(
        "gateway_mcp_servers",
        sa.Column("catalog_imported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_gateway_mcp_servers_auth_scope",
        "gateway_mcp_servers",
        "auth_scope IN ('none', 'personal', 'legacy_shared')",
    )
    op.create_table(
        "gateway_mcp_user_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("server_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="connected", nullable=False),
        sa.Column("auth_type", sa.String(), server_default="bearer_token", nullable=False),
        sa.Column("access_secret_name", sa.String(), nullable=False),
        sa.Column("refresh_secret_name", sa.String(), nullable=True),
        sa.Column("scopes", _json_type(), nullable=False),
        sa.Column("audience", sa.String(), nullable=True),
        sa.Column("oauth_token_endpoint", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["server_id"], ["gateway_mcp_servers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_id", "user_id", name="uq_gateway_mcp_user_connection"
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'error')",
            name="ck_gateway_mcp_user_connection_status",
        ),
    )
    op.create_index(
        "ix_gateway_mcp_user_connections_workspace_user",
        "gateway_mcp_user_connections",
        ["workspace_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gateway_mcp_user_connections_workspace_user",
        table_name="gateway_mcp_user_connections",
    )
    op.drop_table("gateway_mcp_user_connections")
    op.drop_constraint(
        "ck_gateway_mcp_servers_auth_scope", "gateway_mcp_servers", type_="check"
    )
    for column in (
        "catalog_imported_at",
        "catalog_digest",
        "catalog_version",
        "catalog_artifact_name",
        "catalog_source_id",
        "auth_scope",
    ):
        op.drop_column("gateway_mcp_servers", column)
    op.drop_constraint("uq_gateway_tools_ws_target_name", "gateway_tools", type_="unique")
    op.create_unique_constraint(
        "uq_gateway_tools_ws_target_name",
        "gateway_tools",
        ["workspace_id", "scope_type", "target_id", "tool_name"],
    )
    op.drop_constraint("fk_gateway_tools_server_id", "gateway_tools", type_="foreignkey")
    op.drop_column("gateway_tools", "server_id")
    op.drop_index(
        "ix_gateway_catalog_artifacts_workspace_kind",
        table_name="gateway_catalog_artifacts",
    )
    op.drop_table("gateway_catalog_artifacts")
    op.drop_index(
        "ix_gateway_catalog_bindings_source", table_name="gateway_catalog_bindings"
    )
    op.drop_table("gateway_catalog_bindings")
    op.drop_index(
        "ix_gateway_catalog_sources_workspace", table_name="gateway_catalog_sources"
    )
    op.drop_table("gateway_catalog_sources")
