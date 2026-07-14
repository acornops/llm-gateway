"""Store advertised MCP tool output schemas.

Revision ID: c84d9e2a61f0
Revises: b7f06b7e2c1d
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c84d9e2a61f0"
down_revision = "b7f06b7e2c1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gateway_tools",
        sa.Column(
            "output_schema",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column(
        "gateway_tools",
        sa.Column("artifact_policy", sa.String(), nullable=False, server_default="never"),
    )
    op.create_check_constraint(
        "ck_gateway_tools_artifact_policy",
        "gateway_tools",
        "artifact_policy IN ('never', 'if_detailed', 'always')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_gateway_tools_artifact_policy", "gateway_tools", type_="check"
    )
    op.drop_column("gateway_tools", "artifact_policy")
    op.drop_column("gateway_tools", "output_schema")
