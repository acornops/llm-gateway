"""Add explicit workspace and target MCP scopes.

Revision ID: b7f06b7e2c1d
Revises: a10047518e6a
"""

import sqlalchemy as sa

from alembic import op

revision = "b7f06b7e2c1d"
down_revision = "a10047518e6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("gateway_tools", "gateway_mcp_servers"):
        op.add_column(
            table,
            sa.Column("scope_type", sa.String(), nullable=False, server_default="target"),
        )
        op.create_check_constraint(
            f"ck_{table}_scope_type",
            table,
            "scope_type IN ('workspace', 'target')",
        )

    op.drop_constraint("uq_gateway_tools_ws_target_name", "gateway_tools", type_="unique")
    op.create_unique_constraint(
        "uq_gateway_tools_ws_target_name",
        "gateway_tools",
        ["workspace_id", "scope_type", "target_id", "tool_name"],
    )
    op.drop_constraint(
        "uq_gateway_mcp_servers_ws_target_name", "gateway_mcp_servers", type_="unique"
    )
    op.drop_constraint(
        "uq_gateway_mcp_servers_ws_target_url", "gateway_mcp_servers", type_="unique"
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_servers_ws_target_name",
        "gateway_mcp_servers",
        ["workspace_id", "scope_type", "target_id", "server_name"],
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_servers_ws_target_url",
        "gateway_mcp_servers",
        ["workspace_id", "scope_type", "target_id", "server_url"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_gateway_mcp_servers_ws_target_url", "gateway_mcp_servers", type_="unique"
    )
    op.drop_constraint(
        "uq_gateway_mcp_servers_ws_target_name", "gateway_mcp_servers", type_="unique"
    )
    op.drop_constraint("uq_gateway_tools_ws_target_name", "gateway_tools", type_="unique")
    op.create_unique_constraint(
        "uq_gateway_tools_ws_target_name",
        "gateway_tools",
        ["workspace_id", "target_id", "tool_name"],
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_servers_ws_target_name",
        "gateway_mcp_servers",
        ["workspace_id", "target_id", "server_name"],
    )
    op.create_unique_constraint(
        "uq_gateway_mcp_servers_ws_target_url",
        "gateway_mcp_servers",
        ["workspace_id", "target_id", "server_url"],
    )
    for table in ("gateway_tools", "gateway_mcp_servers"):
        op.drop_constraint(f"ck_{table}_scope_type", table, type_="check")
        op.drop_column(table, "scope_type")
