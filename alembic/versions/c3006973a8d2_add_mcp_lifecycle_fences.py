"""Add durable MCP lifecycle fences and credential epochs.

Revision ID: c3006973a8d2
Revises: b20058629f4b

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3006973a8d2"
down_revision: str | Sequence[str] | None = "b20058629f4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    duplicate_target = connection.execute(
        sa.text(
            """
            SELECT workspace_id, target_id, target_type, count(*) AS duplicate_count
            FROM gateway_mcp_servers
            WHERE provenance_type = 'builtin' AND scope_type = 'target'
            GROUP BY workspace_id, target_id, target_type
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    duplicate_agent = connection.execute(
        sa.text(
            """
            SELECT workspace_id, agent_id, count(*) AS duplicate_count
            FROM gateway_mcp_servers
            WHERE provenance_type = 'builtin' AND scope_type = 'agent'
            GROUP BY workspace_id, agent_id
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate_target is not None or duplicate_agent is not None:
        raise RuntimeError(
            "Duplicate built-in MCP servers must be reconciled before migration c3006973a8d2"
        )

    op.create_index(
        "ix_gateway_secrets_tenant_scope_name",
        "gateway_secrets",
        ["tenant_scope", "secret_name"],
        unique=False,
    )

    op.drop_index(
        "uq_gateway_mcp_servers_builtin_destination",
        table_name="gateway_mcp_servers",
    )
    op.create_index(
        "uq_gateway_mcp_servers_builtin_target_destination",
        "gateway_mcp_servers",
        ["workspace_id", "target_id", "target_type"],
        unique=True,
        postgresql_where=sa.text(
            "provenance_type='builtin' AND scope_type='target'"
        ),
        sqlite_where=sa.text("provenance_type='builtin' AND scope_type='target'"),
    )
    op.create_index(
        "uq_gateway_mcp_servers_builtin_agent_destination",
        "gateway_mcp_servers",
        ["workspace_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text(
            "provenance_type='builtin' AND scope_type='agent'"
        ),
        sqlite_where=sa.text("provenance_type='builtin' AND scope_type='agent'"),
    )
    op.add_column(
        "gateway_mcp_servers",
        sa.Column("credential_epoch", sa.Integer(), server_default="1", nullable=False),
    )
    op.alter_column("gateway_mcp_servers", "credential_epoch", server_default=None)
    op.add_column(
        "gateway_mcp_connections",
        sa.Column("membership_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "gateway_catalog_sources",
        sa.Column(
            "authority_generation",
            sa.BigInteger(),
            server_default="1",
            nullable=False,
        ),
    )
    op.alter_column(
        "gateway_catalog_sources", "authority_generation", server_default=None
    )
    op.create_check_constraint(
        "ck_gateway_catalog_source_authority_generation_positive",
        "gateway_catalog_sources",
        "authority_generation > 0",
    )
    op.add_column(
        "gateway_catalog_sources",
        sa.Column(
            "credential_transitioning",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.alter_column(
        "gateway_catalog_sources", "credential_transitioning", server_default=None
    )
    op.add_column(
        "gateway_catalog_sources",
        sa.Column("previous_auth_secret_name", sa.String(), nullable=True),
    )
    op.add_column(
        "gateway_catalog_bindings",
        sa.Column(
            "sync_generation",
            sa.BigInteger(),
            server_default="1",
            nullable=False,
        ),
    )
    op.alter_column(
        "gateway_catalog_bindings", "sync_generation", server_default=None
    )
    op.create_check_constraint(
        "ck_gateway_catalog_binding_sync_generation_positive",
        "gateway_catalog_bindings",
        "sync_generation > 0",
    )
    op.create_check_constraint(
        "ck_gateway_mcp_connection_generation_positive",
        "gateway_mcp_connections",
        "membership_generation IS NULL OR "
        "(membership_generation > 0 AND membership_generation <= 9007199254740991)",
    )
    op.create_table(
        "gateway_mcp_lifecycle_fences",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("fence_key", sa.String(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("destination_id", sa.String(), nullable=True),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('workspace','agent','target')",
            name="ck_gateway_mcp_lifecycle_fence_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'workspace' AND destination_id IS NULL AND target_type IS NULL) OR "
            "(scope_type = 'agent' AND destination_id IS NOT NULL AND target_type IS NULL) OR "
            "(scope_type = 'target' AND destination_id IS NOT NULL AND target_type IS NOT NULL)",
            name="ck_gateway_mcp_lifecycle_fence_scope_shape",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "fence_key"),
    )
    op.create_index(
        "ix_gateway_mcp_lifecycle_fences_workspace",
        "gateway_mcp_lifecycle_fences",
        ["workspace_id"],
        unique=False,
    )
    op.create_table(
        "gateway_mcp_user_lifecycles",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("membership_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "membership_generation > 0 AND membership_generation <= 9007199254740991",
            name="ck_gateway_mcp_user_lifecycle_generation_positive",
        ),
        sa.CheckConstraint(
            "status IN ('active','removed','activating')",
            name="ck_gateway_mcp_user_lifecycle_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )
    op.create_index(
        "ix_gateway_mcp_user_lifecycles_workspace",
        "gateway_mcp_user_lifecycles",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gateway_mcp_user_lifecycles_workspace",
        table_name="gateway_mcp_user_lifecycles",
    )
    op.drop_table("gateway_mcp_user_lifecycles")
    op.drop_index(
        "ix_gateway_mcp_lifecycle_fences_workspace",
        table_name="gateway_mcp_lifecycle_fences",
    )
    op.drop_table("gateway_mcp_lifecycle_fences")
    op.drop_constraint(
        "ck_gateway_mcp_connection_generation_positive",
        "gateway_mcp_connections",
        type_="check",
    )
    op.drop_constraint(
        "ck_gateway_catalog_source_authority_generation_positive",
        "gateway_catalog_sources",
        type_="check",
    )
    op.drop_constraint(
        "ck_gateway_catalog_binding_sync_generation_positive",
        "gateway_catalog_bindings",
        type_="check",
    )
    op.drop_column("gateway_catalog_bindings", "sync_generation")
    op.drop_column("gateway_catalog_sources", "previous_auth_secret_name")
    op.drop_column("gateway_catalog_sources", "credential_transitioning")
    op.drop_column("gateway_catalog_sources", "authority_generation")
    op.drop_column("gateway_mcp_connections", "membership_generation")
    op.drop_column("gateway_mcp_servers", "credential_epoch")
    op.drop_index(
        "ix_gateway_secrets_tenant_scope_name",
        table_name="gateway_secrets",
    )
    op.drop_index(
        "uq_gateway_mcp_servers_builtin_agent_destination",
        table_name="gateway_mcp_servers",
    )
    op.drop_index(
        "uq_gateway_mcp_servers_builtin_target_destination",
        table_name="gateway_mcp_servers",
    )
    op.create_index(
        "uq_gateway_mcp_servers_builtin_destination",
        "gateway_mcp_servers",
        ["workspace_id", "scope_type", "target_id", "target_type"],
        unique=True,
        postgresql_where=sa.text("provenance_type='builtin'"),
        sqlite_where=sa.text("provenance_type='builtin'"),
    )
