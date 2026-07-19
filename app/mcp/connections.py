from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.mcp.registry.models import McpServer, McpUserConnection


class McpConnectionStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def mutation_lock(
        self, workspace_id: str, server_id: str, user_id: str
    ) -> AsyncIterator[None]:
        """Serialize one personal connection across gateway replicas."""
        lock_material = f"{workspace_id}\0{server_id}\0{user_id}".encode()
        lock_key = int.from_bytes(
            hashlib.blake2b(lock_material, digest_size=8).digest(),
            byteorder="big",
            signed=True,
        )
        async with self.engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            try:
                yield
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )

    async def get(
        self, workspace_id: str, server_id: str, user_id: str
    ) -> McpUserConnection | None:
        try:
            normalized = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None
        async with self.async_session() as session:
            return (
                await session.execute(
                    select(McpUserConnection).where(
                        McpUserConnection.workspace_id == workspace_id,
                        McpUserConnection.server_id == normalized,
                        McpUserConnection.user_id == user_id,
                    )
                )
            ).scalars().first()

    async def upsert(
        self,
        *,
        workspace_id: str,
        server_id: str,
        user_id: str,
        access_secret_name: str,
        status: str,
        verified_tool_names: list[str] | None = None,
        error_code: str | None = None,
    ) -> McpUserConnection | None:
        try:
            normalized = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None
        async with self.async_session() as session:
            server = (
                await session.execute(
                    select(McpServer).where(
                        McpServer.id == normalized,
                        McpServer.workspace_id == workspace_id,
                    )
                )
            ).scalars().first()
            if server is None or server.auth_scope != "personal":
                return None
            connection = (
                await session.execute(
                    select(McpUserConnection).where(
                        McpUserConnection.server_id == normalized,
                        McpUserConnection.user_id == user_id,
                    )
                )
            ).scalars().first()
            if connection is None:
                connection = McpUserConnection(
                    workspace_id=workspace_id,
                    server_id=normalized,
                    user_id=user_id,
                    access_secret_name=access_secret_name,
                )
                session.add(connection)
            connection.status = status
            connection.access_secret_name = access_secret_name
            connection.verified_tool_names = sorted(set(verified_tool_names or []))
            connection.verified_at = datetime.now(UTC) if status == "connected" else None
            connection.error_code = error_code if status == "error" else None
            await session.commit()
            await session.refresh(connection)
            return connection

    async def set_state(
        self,
        connection: McpUserConnection,
        status: str,
        *,
        verified_tool_names: list[str] | None = None,
        error_code: str | None = None,
    ) -> McpUserConnection | None:
        async with self.async_session() as session:
            persisted = await session.get(McpUserConnection, connection.id)
            if persisted is None:
                return None
            persisted.status = status
            persisted.verified_tool_names = sorted(set(verified_tool_names or []))
            persisted.verified_at = datetime.now(UTC) if status == "connected" else None
            persisted.error_code = error_code if status == "error" else None
            await session.commit()
            await session.refresh(persisted)
            return persisted

    async def set_status(
        self, connection: McpUserConnection, status: str
    ) -> McpUserConnection | None:
        """Compatibility wrapper that always clears stale per-user discovery."""
        return await self.set_state(
            connection,
            status,
            error_code="MCP_PAT_CONNECTION_ERROR" if status == "error" else None,
        )

    async def list_for_server(
        self, workspace_id: str, server_id: str
    ) -> list[McpUserConnection]:
        try:
            normalized = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return []
        async with self.async_session() as session:
            return list(
                (
                    await session.execute(
                        select(McpUserConnection).where(
                            McpUserConnection.workspace_id == workspace_id,
                            McpUserConnection.server_id == normalized,
                        )
                    )
                ).scalars().all()
            )

    async def list_for_principal(
        self, workspace_id: str, user_id: str
    ) -> list[McpUserConnection]:
        async with self.async_session() as session:
            return list(
                (
                    await session.execute(
                        select(McpUserConnection).where(
                            McpUserConnection.workspace_id == workspace_id,
                            McpUserConnection.user_id == user_id,
                        )
                    )
                ).scalars().all()
            )

    async def list_for_workspace(self, workspace_id: str) -> list[McpUserConnection]:
        async with self.async_session() as session:
            return list(
                (
                    await session.execute(
                        select(McpUserConnection).where(
                            McpUserConnection.workspace_id == workspace_id
                        )
                    )
                ).scalars().all()
            )

    async def delete(
        self, workspace_id: str, server_id: str, user_id: str
    ) -> bool:
        try:
            normalized = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return False
        async with self.async_session() as session:
            result = await session.execute(
                delete(McpUserConnection).where(
                    McpUserConnection.workspace_id == workspace_id,
                    McpUserConnection.server_id == normalized,
                    McpUserConnection.user_id == user_id,
                )
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def is_ready(connection: McpUserConnection | None) -> bool:
        return connection is not None and connection.status == "connected"

    @staticmethod
    def has_verified_tool(
        connection: McpUserConnection | None, tool_name: str
    ) -> bool:
        return bool(
            connection is not None
            and connection.status == "connected"
            and tool_name in (connection.verified_tool_names or [])
        )


mcp_connection_store = McpConnectionStore(settings.DATABASE_URL)
