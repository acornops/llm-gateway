"""Export a secret-free capability inventory before the additive migration."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal, UUID)):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


async def _export() -> dict[str, object]:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://gateway_user:gateway_password@localhost:5432/gateway",
    )
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            servers = (
                await connection.execute(
                    text(
                        """
                        SELECT server.workspace_id, server.id, server.scope_type,
                               server.target_id, server.target_type,
                               server.server_name, server.enabled, server.auth_type,
                               server.auth_scope, server.catalog_source_id,
                               server.catalog_artifact_name, server.catalog_version,
                               EXISTS (
                                 SELECT 1 FROM gateway_tools AS tool
                                 WHERE tool.server_id = server.id
                                   AND tool.source = 'builtin'
                               ) AS contains_builtin_tools
                        FROM gateway_mcp_servers AS server
                        ORDER BY server.workspace_id, server.scope_type,
                                 server.target_id, server.server_name, server.id
                        """
                    )
                )
            ).mappings().all()
            tools = (
                await connection.execute(
                    text(
                        """
                        SELECT tool.workspace_id, tool.server_id, tool.tool_name,
                               tool.enabled, tool.capability, tool.source,
                               tool.version
                        FROM gateway_tools AS tool
                        ORDER BY tool.workspace_id, tool.server_id, tool.tool_name
                        """
                    )
                )
            ).mappings().all()
            connections = (
                await connection.execute(
                    text(
                        """
                        SELECT connection.workspace_id, connection.server_id,
                               connection.user_id, connection.status
                        FROM gateway_mcp_user_connections AS connection
                        ORDER BY connection.workspace_id, connection.server_id,
                                 connection.user_id
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(),
        "migrationMode": "additive",
        "preservesTargetCapabilities": True,
        "secretFree": True,
        "legacy": {
            "mcpServers": [dict(row) for row in servers],
            "tools": [dict(row) for row in tools],
            "userConnections": [dict(row) for row in connections],
        },
    }


def main() -> None:
    print(json.dumps(asyncio.run(_export()), indent=2, default=_json_default))


if __name__ == "__main__":
    main()
