"""Initial target-scoped schema

Revision ID: a10047518e6a
Revises:
Create Date: 2026-02-18 08:16:15.824190

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a10047518e6a"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the fresh llm-gateway schema."""
    op.create_table(
        "gateway_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "tenant_scope",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("secret_name", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("aad", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "gateway_tools",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("mcp_server_url", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column(
            "input_schema",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "target_id", "tool_name",
            name="uq_gateway_tools_ws_target_name",
        ),
    )
    op.create_index(
        "ix_gateway_tools_workspace_target",
        "gateway_tools",
        ["workspace_id", "target_id"],
        unique=False,
    )
    op.create_table(
        "gateway_mcp_servers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("server_name", sa.String(), nullable=False),
        sa.Column("server_url", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("auth_type", sa.String(), nullable=False),
        sa.Column("auth_secret_name", sa.String(), nullable=True),
        sa.Column("auth_header_name", sa.String(), nullable=True),
        sa.Column("auth_header_prefix", sa.String(), nullable=True),
        sa.Column(
            "public_headers",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("connection_status", sa.String(), nullable=False),
        sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_discovery_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "target_id", "server_name",
            name="uq_gateway_mcp_servers_ws_target_name",
        ),
        sa.UniqueConstraint(
            "workspace_id", "target_id", "server_url",
            name="uq_gateway_mcp_servers_ws_target_url",
        ),
    )
    op.create_index(
        "ix_gateway_mcp_servers_workspace_target",
        "gateway_mcp_servers",
        ["workspace_id", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the fresh llm-gateway schema."""
    op.drop_index("ix_gateway_mcp_servers_workspace_target", table_name="gateway_mcp_servers")
    op.drop_table("gateway_mcp_servers")
    op.drop_index("ix_gateway_tools_workspace_target", table_name="gateway_tools")
    op.drop_table("gateway_tools")
    op.drop_table("gateway_secrets")
