"""Reset pre-release MCP connections to the PAT-only V1 schema.

Revision ID: f6c21b8a9d44
Revises: e8f3c274a8c1
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f6c21b8a9d44"
down_revision = "e8f3c274a8c1"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # V1 deliberately does not preserve pre-release OAuth, service-principal,
    # or PAT connection data. Database-backed secrets are removed in the same
    # transaction; operators must separately clear the documented Vault path.
    op.execute(
        """
        DELETE FROM gateway_secrets
        WHERE secret_name LIKE 'mcp_oauth_state::%'
           OR secret_name LIKE 'mcp_principal::%'
           OR secret_name LIKE 'mcp_user::%'
           OR secret_name LIKE 'mcp_pat::%'
        """
    )
    op.execute("DELETE FROM gateway_mcp_user_connections")
    op.alter_column(
        "gateway_mcp_user_connections",
        "status",
        server_default="error",
        existing_type=sa.String(),
        existing_nullable=False,
    )
    # Workspace-scoped external MCP was a pre-release service-connection model.
    # V1 installations are owned by an exact target or Agent.
    op.execute("DELETE FROM gateway_mcp_servers WHERE scope_type = 'workspace'")

    # Every authenticated installation becomes personal and must not retain a
    # destination-wide credential. Platform-owned built-in bridges use
    # auth_type=none and are therefore unchanged.
    op.execute(
        """
        DELETE FROM gateway_secrets
        WHERE secret_name IN (
          SELECT server.auth_secret_name
          FROM gateway_mcp_servers AS server
          WHERE server.scope_type IN ('agent', 'target')
            AND server.auth_type IN ('bearer_token', 'custom_header')
            AND server.auth_secret_name IS NOT NULL
        )
        """
    )
    op.execute(
        """
        UPDATE gateway_mcp_servers AS server
        SET auth_scope = 'personal', auth_secret_name = NULL
        WHERE server.scope_type IN ('agent', 'target')
          AND server.auth_type IN ('bearer_token', 'custom_header')
        """
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
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column(
            "verified_tool_names",
            _json_type(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_gateway_mcp_user_connection_error_code",
        "gateway_mcp_user_connections",
        "error_code IS NULL OR length(error_code) <= 64",
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_servers_id_workspace",
        "gateway_mcp_servers",
        ["id", "workspace_id"],
    )
    op.drop_constraint(
        "gateway_mcp_user_connections_server_id_fkey",
        "gateway_mcp_user_connections",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_gateway_mcp_user_connection_server_workspace",
        "gateway_mcp_user_connections",
        "gateway_mcp_servers",
        ["server_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "ck_gateway_mcp_servers_scope_type", "gateway_mcp_servers", type_="check"
    )
    op.drop_constraint(
        "ck_gateway_tools_scope_type", "gateway_tools", type_="check"
    )
    op.create_check_constraint(
        "ck_gateway_mcp_servers_scope_type",
        "gateway_mcp_servers",
        "scope_type IN ('agent', 'target')",
    )
    op.create_check_constraint(
        "ck_gateway_tools_scope_type",
        "gateway_tools",
        "scope_type IN ('agent', 'target')",
    )
    for column in (
        "oauth_grant_type",
        "oauth_token_endpoint",
        "provider_tenant",
        "provider_account_id",
        "client_id",
        "issuer",
        "audience",
        "expires_at",
        "scopes",
        "refresh_secret_name",
        "auth_type",
        "principal_type",
    ):
        op.drop_column("gateway_mcp_user_connections", column)


def downgrade() -> None:
    op.alter_column(
        "gateway_mcp_user_connections",
        "status",
        server_default="connected",
        existing_type=sa.String(),
        existing_nullable=False,
    )
    op.drop_constraint(
        "ck_gateway_tools_scope_type", "gateway_tools", type_="check"
    )
    op.drop_constraint(
        "ck_gateway_mcp_servers_scope_type", "gateway_mcp_servers", type_="check"
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
    op.drop_constraint(
        "fk_gateway_mcp_user_connection_server_workspace",
        "gateway_mcp_user_connections",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "gateway_mcp_user_connections_server_id_fkey",
        "gateway_mcp_user_connections",
        "gateway_mcp_servers",
        ["server_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_gateway_mcp_servers_id_workspace",
        "gateway_mcp_servers",
        type_="unique",
    )
    op.drop_constraint(
        "ck_gateway_mcp_user_connection_error_code",
        "gateway_mcp_user_connections",
        type_="check",
    )
    for column in ("error_code", "verified_at", "verified_tool_names"):
        op.drop_column("gateway_mcp_user_connections", column)
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("principal_type", sa.String(), server_default="user", nullable=False),
    )
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("auth_type", sa.String(), server_default="bearer_token", nullable=False),
    )
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("refresh_secret_name", sa.String(), nullable=True),
    )
    op.add_column(
        "gateway_mcp_user_connections",
        sa.Column("scopes", _json_type(), server_default=sa.text("'[]'"), nullable=False),
    )
    for column in (
        "expires_at",
        "audience",
        "issuer",
        "client_id",
        "provider_account_id",
        "provider_tenant",
        "oauth_token_endpoint",
        "oauth_grant_type",
    ):
        column_type = sa.DateTime(timezone=True) if column == "expires_at" else sa.String()
        op.add_column(
            "gateway_mcp_user_connections",
            sa.Column(column, column_type, nullable=True),
        )
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
