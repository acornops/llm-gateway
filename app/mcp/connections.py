from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.mcp.registry.models import McpConnection, McpServer
from app.outbound_tls import sqlalchemy_connection_config

ConnectionOwnerType = Literal["installation", "user"]
INSTALLATION_OWNER_ID = "installation"


class ConnectionOwnerError(ValueError):
    """Raised when an installation cannot resolve a credential owner."""


@dataclass(frozen=True)
class ConnectionOwner:
    owner_type: ConnectionOwnerType
    owner_id: str


def credential_secret_name(
    workspace_id: str,
    server_id: str,
    owner: ConnectionOwner,
) -> str:
    """Return the only valid secret identity for an MCP connection owner."""
    base = f"mcp_credential::{workspace_id}::{server_id}"
    if owner.owner_type == "installation":
        if owner.owner_id != INSTALLATION_OWNER_ID:
            raise ConnectionOwnerError("installation owner ID is not canonical")
        return f"{base}::installation"
    if not owner.owner_id:
        raise ConnectionOwnerError("user owner ID is required")
    return f"{base}::user::{owner.owner_id}"


def resolve_connection_owner(
    server: McpServer,
    principal_type: str | None,
    principal_id: str | None,
) -> ConnectionOwner | None:
    """Resolve the exact credential owner without fallback between modes."""
    mode = getattr(server, "credential_mode", "none")
    if mode == "none":
        return None
    if mode == "workspace":
        return ConnectionOwner("installation", INSTALLATION_OWNER_ID)
    if mode == "individual":
        if principal_type != "user" or not principal_id:
            raise ConnectionOwnerError(
                "individual MCP credentials require a user principal"
            )
        return ConnectionOwner("user", principal_id)
    raise ConnectionOwnerError("unsupported MCP credential mode")


class McpConnectionStore:
    def __init__(self, database_url: str) -> None:
        database_url, connect_args = sqlalchemy_connection_config(database_url)
        self.engine = create_async_engine(database_url, connect_args=connect_args)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._mutation_locks: dict[
            tuple[str, str, str, str],
            tuple[asyncio.Lock, int],
        ] = {}
        self._mutation_locks_guard = asyncio.Lock()

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def mutation_lock(
        self,
        workspace_id: str,
        server_id: str,
        owner: ConnectionOwner,
    ) -> AsyncIterator[None]:
        """Serialize one connection owner across gateway replicas."""
        key = (workspace_id, server_id, owner.owner_type, owner.owner_id)
        async with self._mutation_locks_guard:
            lock, users = self._mutation_locks.get(key, (asyncio.Lock(), 0))
            self._mutation_locks[key] = (lock, users + 1)
        try:
            async with lock:
                runtime_env = (settings.NODE_ENV or settings.APP_ENV).strip().lower()
                if runtime_env != "production":
                    yield
                    return
                lock_material = "\0".join(key).encode()
                lock_key = int.from_bytes(
                    hashlib.blake2b(lock_material, digest_size=8).digest(),
                    byteorder="big",
                    signed=True,
                )
                async with (
                    self.engine.connect() as connection,
                    connection.begin(),
                ):
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )
                    yield
        finally:
            async with self._mutation_locks_guard:
                current = self._mutation_locks.get(key)
                if current is not None and current[0] is lock:
                    remaining = current[1] - 1
                    if remaining == 0:
                        self._mutation_locks.pop(key, None)
                    else:
                        self._mutation_locks[key] = (lock, remaining)

    @staticmethod
    def _server_uuid(server_id: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None

    async def get(
        self, workspace_id: str, server_id: str, owner: ConnectionOwner
    ) -> McpConnection | None:
        normalized = self._server_uuid(server_id)
        if normalized is None:
            return None
        async with self.async_session() as session:
            return (
                await session.execute(
                    select(McpConnection).where(
                        McpConnection.workspace_id == workspace_id,
                        McpConnection.server_id == normalized,
                        McpConnection.owner_type == owner.owner_type,
                        McpConnection.owner_id == owner.owner_id,
                    )
                )
            ).scalars().first()

    async def upsert(
        self,
        *,
        workspace_id: str,
        server_id: str,
        owner: ConnectionOwner,
        status: str,
        verified_tool_names: list[str] | None = None,
        error_code: str | None = None,
        oauth_issuer: str | None = None,
        oauth_registration_method: str | None = None,
        oauth_resource: str | None = None,
        oauth_client_id: str | None = None,
        oauth_endpoint_snapshot: dict[str, object] | None = None,
        oauth_scopes: list[str] | None = None,
        oauth_token_expires_at: datetime | None = None,
        oauth_refresh_capable: bool = False,
    ) -> McpConnection | None:
        normalized = self._server_uuid(server_id)
        if normalized is None:
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
            if server is None:
                return None
            try:
                expected_owner = resolve_connection_owner(
                    server,
                    "user" if owner.owner_type == "user" else "service_identity",
                    owner.owner_id if owner.owner_type == "user" else None,
                )
            except ConnectionOwnerError:
                return None
            if expected_owner != owner:
                return None
            connection = (
                await session.execute(
                    select(McpConnection).where(
                        McpConnection.server_id == normalized,
                        McpConnection.owner_type == owner.owner_type,
                        McpConnection.owner_id == owner.owner_id,
                    )
                )
            ).scalars().first()
            if connection is None:
                connection = McpConnection(
                    workspace_id=workspace_id,
                    server_id=normalized,
                    owner_type=owner.owner_type,
                    owner_id=owner.owner_id,
                )
                session.add(connection)
            connection.status = status
            connection.verified_tool_names = sorted(set(verified_tool_names or []))
            connection.verified_at = datetime.now(UTC) if status == "connected" else None
            connection.error_code = error_code if status != "connected" else None
            connection.oauth_issuer = oauth_issuer
            connection.oauth_registration_method = oauth_registration_method
            connection.oauth_resource = oauth_resource
            connection.oauth_client_id = oauth_client_id
            connection.oauth_endpoint_snapshot = oauth_endpoint_snapshot
            connection.oauth_scopes = sorted(set(oauth_scopes or []))
            connection.oauth_token_expires_at = oauth_token_expires_at
            connection.oauth_refresh_capable = oauth_refresh_capable
            await session.commit()
            await session.refresh(connection)
            return connection

    async def set_state(
        self,
        connection: McpConnection,
        status: str,
        *,
        verified_tool_names: list[str] | None = None,
        error_code: str | None = None,
        oauth_issuer: str | None = None,
        oauth_registration_method: str | None = None,
        oauth_resource: str | None = None,
        oauth_client_id: str | None = None,
        oauth_endpoint_snapshot: dict[str, object] | None = None,
        oauth_scopes: list[str] | None = None,
        oauth_token_expires_at: datetime | None = None,
        oauth_refresh_capable: bool | None = None,
    ) -> McpConnection | None:
        async with self.async_session() as session:
            persisted = await session.get(McpConnection, connection.id)
            if persisted is None:
                return None
            persisted.status = status
            persisted.verified_tool_names = sorted(set(verified_tool_names or []))
            persisted.verified_at = datetime.now(UTC) if status == "connected" else None
            persisted.error_code = error_code if status != "connected" else None
            if oauth_issuer is not None:
                persisted.oauth_issuer = oauth_issuer
            if oauth_registration_method is not None:
                persisted.oauth_registration_method = oauth_registration_method
            if oauth_resource is not None:
                persisted.oauth_resource = oauth_resource
            if oauth_client_id is not None:
                persisted.oauth_client_id = oauth_client_id
            if oauth_endpoint_snapshot is not None:
                persisted.oauth_endpoint_snapshot = oauth_endpoint_snapshot
            if oauth_scopes is not None:
                persisted.oauth_scopes = sorted(set(oauth_scopes))
            if oauth_token_expires_at is not None or status != "connected":
                persisted.oauth_token_expires_at = oauth_token_expires_at
            if oauth_refresh_capable is not None:
                persisted.oauth_refresh_capable = oauth_refresh_capable
            await session.commit()
            await session.refresh(persisted)
            return persisted

    async def list_for_server(
        self, workspace_id: str, server_id: str
    ) -> list[McpConnection]:
        normalized = self._server_uuid(server_id)
        if normalized is None:
            return []
        async with self.async_session() as session:
            return list(
                (
                    await session.execute(
                        select(McpConnection).where(
                            McpConnection.workspace_id == workspace_id,
                            McpConnection.server_id == normalized,
                        )
                    )
                ).scalars().all()
            )

    async def list_for_user(
        self, workspace_id: str, user_id: str
    ) -> list[McpConnection]:
        async with self.async_session() as session:
            return list(
                (
                    await session.execute(
                        select(McpConnection).where(
                            McpConnection.workspace_id == workspace_id,
                            McpConnection.owner_type == "user",
                            McpConnection.owner_id == user_id,
                        )
                    )
                ).scalars().all()
            )

    async def list_for_workspace(self, workspace_id: str) -> list[McpConnection]:
        async with self.async_session() as session:
            return list(
                (
                    await session.execute(
                        select(McpConnection).where(
                            McpConnection.workspace_id == workspace_id
                        )
                    )
                ).scalars().all()
            )

    async def delete(
        self, workspace_id: str, server_id: str, owner: ConnectionOwner
    ) -> bool:
        normalized = self._server_uuid(server_id)
        if normalized is None:
            return False
        async with self.async_session() as session:
            result = await session.execute(
                delete(McpConnection).where(
                    McpConnection.workspace_id == workspace_id,
                    McpConnection.server_id == normalized,
                    McpConnection.owner_type == owner.owner_type,
                    McpConnection.owner_id == owner.owner_id,
                )
            )
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def is_ready(connection: McpConnection | None) -> bool:
        return connection is not None and connection.status == "connected"

    @staticmethod
    def has_verified_tool(connection: McpConnection | None, tool_name: str) -> bool:
        return bool(
            connection is not None
            and connection.status == "connected"
            and tool_name in (connection.verified_tool_names or [])
        )


mcp_connection_store = McpConnectionStore(settings.DATABASE_URL)
