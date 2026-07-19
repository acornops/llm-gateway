"""Add one-use approval receipts and stable built-in server identity.

Revision ID: a2b4d6e8f010
Revises: f6c21b8a9d44
"""

import sqlalchemy as sa

from alembic import op

revision = "a2b4d6e8f010"
down_revision = "f6c21b8a9d44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM gateway_mcp_servers server
            JOIN gateway_tools tool ON tool.server_id=server.id AND tool.source='builtin'
            GROUP BY server.workspace_id,server.scope_type,server.target_id,server.target_type
            HAVING COUNT(DISTINCT server.id)>1
          ) THEN
            RAISE EXCEPTION USING MESSAGE='MCP_DUPLICATE_BUILTIN_SERVER_ANOMALY';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        UPDATE gateway_mcp_servers server SET provenance_type='builtin'
        WHERE EXISTS (
          SELECT 1 FROM gateway_tools tool
          WHERE tool.server_id=server.id AND tool.source='builtin'
        )
        """
    )
    op.create_check_constraint(
        "ck_gateway_mcp_servers_provenance_type",
        "gateway_mcp_servers",
        "provenance_type IN ('manual','catalog','builtin')",
    )
    op.create_index(
        "uq_gateway_mcp_servers_builtin_destination",
        "gateway_mcp_servers",
        ["workspace_id", "scope_type", "target_id", "target_type"],
        unique=True,
        postgresql_where=sa.text("provenance_type='builtin'"),
        sqlite_where=sa.text("provenance_type='builtin'"),
    )
    op.create_table(
        "gateway_approval_receipt_uses",
        sa.Column("jti", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(
        "ix_gateway_approval_receipt_uses_expires_at",
        "gateway_approval_receipt_uses",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gateway_approval_receipt_uses_expires_at", table_name="gateway_approval_receipt_uses"
    )
    op.drop_table("gateway_approval_receipt_uses")
    op.drop_index("uq_gateway_mcp_servers_builtin_destination", table_name="gateway_mcp_servers")
    op.drop_constraint(
        "ck_gateway_mcp_servers_provenance_type", "gateway_mcp_servers", type_="check"
    )
    op.execute(
        "UPDATE gateway_mcp_servers SET provenance_type='manual' WHERE provenance_type='builtin'"
    )
