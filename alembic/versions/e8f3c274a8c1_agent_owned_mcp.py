"""Add Agent-owned MCP installations alongside target-owned installations.

Revision ID: e8f3c274a8c1
Revises: d4a91c2e7b30
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e8f3c274a8c1"
down_revision = "d4a91c2e7b30"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("gateway_mcp_servers", sa.Column("agent_id", sa.String(), nullable=True))
    op.add_column(
        "gateway_mcp_servers",
        sa.Column("provenance_type", sa.String(), server_default="manual", nullable=False),
    )
    op.add_column(
        "gateway_mcp_servers",
        sa.Column(
            "endpoint_configuration",
            _json_type(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "gateway_mcp_servers",
        sa.Column(
            "target_constraints",
            _json_type(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "gateway_mcp_servers",
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("gateway_tools", sa.Column("agent_id", sa.String(), nullable=True))
    op.add_column(
        "gateway_tools",
        sa.Column("review_state", sa.String(), server_default="pending", nullable=False),
    )
    op.add_column(
        "gateway_tools",
        sa.Column("risk_level", sa.String(), server_default="high_risk", nullable=False),
    )
    op.add_column(
        "gateway_tools",
        sa.Column("auto_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("principal_type", sa.String(), server_default="user", nullable=False),
    )
    for column in (
        "issuer",
        "client_id",
        "provider_account_id",
        "provider_tenant",
        "oauth_grant_type",
    ):
        op.add_column("gateway_mcp_user_connections", sa.Column(column, sa.String(), nullable=True))
    op.drop_constraint(
        "uq_gateway_mcp_user_connection",
        "gateway_mcp_user_connections",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_user_connection",
        "gateway_mcp_user_connections",
        ["server_id", "principal_type", "user_id"],
    )
    op.create_check_constraint(
        "ck_gateway_mcp_connection_principal_type",
        "gateway_mcp_user_connections",
        "principal_type IN ('user', 'service_identity')",
    )

    # Existing target tools were explicitly enabled under the target-owned
    # review flow. Preserve that decision while new discoveries remain pending.
    op.execute(
        """
        UPDATE gateway_tools
        SET review_state = CASE
              WHEN enabled OR source = 'builtin' THEN 'approved'
              ELSE 'pending'
            END,
            risk_level = CASE WHEN capability = 'read' THEN 'read_only' ELSE 'high_risk' END,
            auto_allowed = false
        """
    )
    op.execute(
        "UPDATE gateway_mcp_servers SET auth_scope = 'none' WHERE auth_scope = 'legacy_shared'"
    )
    op.drop_constraint("ck_gateway_mcp_servers_auth_scope", "gateway_mcp_servers", type_="check")
    op.create_check_constraint(
        "ck_gateway_mcp_servers_auth_scope",
        "gateway_mcp_servers",
        "auth_scope IN ('none', 'personal')",
    )
    op.drop_constraint(
        "ck_gateway_mcp_servers_scope_type",
        "gateway_mcp_servers",
        type_="check",
    )
    op.drop_constraint(
        "ck_gateway_tools_scope_type",
        "gateway_tools",
        type_="check",
    )
    op.create_check_constraint(
        "ck_gateway_mcp_servers_scope_type",
        "gateway_mcp_servers",
        "scope_type IN ('agent', 'target', 'workspace')",
    )
    op.create_check_constraint(
        "ck_gateway_tools_scope_type",
        "gateway_tools",
        "scope_type IN ('agent', 'target', 'workspace')",
    )
    op.create_check_constraint(
        "ck_gateway_mcp_servers_agent_owner",
        "gateway_mcp_servers",
        "(scope_type = 'agent' AND agent_id IS NOT NULL) OR "
        "(scope_type IN ('target', 'workspace') AND agent_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_gateway_tools_agent_owner",
        "gateway_tools",
        "(scope_type = 'agent' AND agent_id IS NOT NULL) OR "
        "(scope_type IN ('target', 'workspace') AND agent_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_gateway_tools_review_state",
        "gateway_tools",
        "review_state IN ('pending', 'approved', 'rejected')",
    )
    op.create_check_constraint(
        "ck_gateway_tools_risk_level",
        "gateway_tools",
        "risk_level IN ('read_only', 'non_destructive_write', 'high_risk', 'destructive')",
    )
    op.create_check_constraint(
        "ck_gateway_tools_auto_allowed",
        "gateway_tools",
        "auto_allowed = false OR "
        "(review_state = 'approved' AND risk_level = 'non_destructive_write')",
    )
    op.create_index(
        "ix_gateway_mcp_servers_workspace_agent",
        "gateway_mcp_servers",
        ["workspace_id", "agent_id", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_gateway_mcp_servers_workspace_agent", table_name="gateway_mcp_servers")
    for constraint, table in (
        ("ck_gateway_tools_auto_allowed", "gateway_tools"),
        ("ck_gateway_tools_risk_level", "gateway_tools"),
        ("ck_gateway_tools_review_state", "gateway_tools"),
        ("ck_gateway_tools_agent_owner", "gateway_tools"),
        ("ck_gateway_mcp_servers_agent_owner", "gateway_mcp_servers"),
        ("ck_gateway_tools_scope_type", "gateway_tools"),
        ("ck_gateway_mcp_servers_scope_type", "gateway_mcp_servers"),
    ):
        op.drop_constraint(constraint, table, type_="check")
    for table in ("gateway_mcp_servers", "gateway_tools"):
        op.create_check_constraint(
            f"ck_{table}_scope_type",
            table,
            "scope_type IN ('workspace', 'target')",
        )
    op.drop_constraint("ck_gateway_mcp_servers_auth_scope", "gateway_mcp_servers", type_="check")
    op.create_check_constraint(
        "ck_gateway_mcp_servers_auth_scope",
        "gateway_mcp_servers",
        "auth_scope IN ('none', 'personal', 'legacy_shared')",
    )
    op.drop_constraint(
        "ck_gateway_mcp_connection_principal_type",
        "gateway_mcp_user_connections",
        type_="check",
    )
    op.drop_constraint(
        "uq_gateway_mcp_user_connection",
        "gateway_mcp_user_connections",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_user_connection",
        "gateway_mcp_user_connections",
        ["server_id", "user_id"],
    )
    for column in (
        "oauth_grant_type",
        "provider_tenant",
        "provider_account_id",
        "client_id",
        "issuer",
        "principal_type",
    ):
        op.drop_column("gateway_mcp_user_connections", column)
    for column in ("auto_allowed", "risk_level", "review_state", "agent_id"):
        op.drop_column("gateway_tools", column)
    for column in (
        "revision",
        "target_constraints",
        "endpoint_configuration",
        "provenance_type",
        "agent_id",
    ):
        op.drop_column("gateway_mcp_servers", column)
