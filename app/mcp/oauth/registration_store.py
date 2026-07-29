"""Persistence for non-secret MCP OAuth client registrations."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.mcp.oauth.models import OAuthEndpointSnapshot, OAuthRegistrationMethod
from app.mcp.registry.models import McpOAuthRegistration, McpServer
from app.outbound_tls import sqlalchemy_connection_config


class OAuthRegistrationStore:
    """Store one reusable public client ID per installation and issuer."""

    def __init__(self, database_url: str) -> None:
        database_url, connect_args = sqlalchemy_connection_config(database_url)
        self.engine = create_async_engine(database_url, connect_args=connect_args)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._locks: dict[tuple[str, str, str], tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def registration_lock(
        self,
        workspace_id: str,
        server_id: str,
        issuer: str,
    ) -> AsyncIterator[None]:
        """Serialize registration across gateway replicas in production."""

        identity = (workspace_id, server_id, issuer)
        async with self._locks_guard:
            lock, users = self._locks.get(identity, (asyncio.Lock(), 0))
            self._locks[identity] = (lock, users + 1)
        try:
            async with lock:
                if (settings.NODE_ENV or settings.APP_ENV).strip().lower() != "production":
                    yield
                    return
                material = "\0".join(identity).encode()
                key = int.from_bytes(
                    hashlib.blake2b(material, digest_size=8).digest(),
                    byteorder="big",
                    signed=True,
                )
                async with (
                    self.engine.connect() as connection,
                    connection.begin(),
                ):
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": key},
                    )
                    yield
        finally:
            async with self._locks_guard:
                current = self._locks.get(identity)
                if current is not None and current[0] is lock:
                    remaining = current[1] - 1
                    if remaining == 0:
                        self._locks.pop(identity, None)
                    else:
                        self._locks[identity] = (lock, remaining)

    @staticmethod
    def _server_uuid(server_id: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None

    async def get(
        self,
        workspace_id: str,
        server_id: str,
        issuer: str,
    ) -> McpOAuthRegistration | None:
        normalized = self._server_uuid(server_id)
        if normalized is None:
            return None
        async with self.async_session() as session:
            return (
                await session.execute(
                    select(McpOAuthRegistration).where(
                        McpOAuthRegistration.workspace_id == workspace_id,
                        McpOAuthRegistration.server_id == normalized,
                        McpOAuthRegistration.issuer == issuer,
                    )
                )
            ).scalars().first()

    async def put(
        self,
        *,
        workspace_id: str,
        server_id: str,
        resource: str,
        issuer: str,
        registration_method: OAuthRegistrationMethod,
        client_id: str,
        endpoint_snapshot: OAuthEndpointSnapshot,
        metadata_fingerprint: str,
        client_metadata_fingerprint: str,
        scopes: list[str],
    ) -> McpOAuthRegistration | None:
        normalized = self._server_uuid(server_id)
        if normalized is None:
            return None
        async with self.async_session() as session:
            server = await session.scalar(
                select(McpServer).where(
                    McpServer.id == normalized,
                    McpServer.workspace_id == workspace_id,
                )
            )
            if server is None:
                return None
            registration = await session.scalar(
                select(McpOAuthRegistration).where(
                    McpOAuthRegistration.workspace_id == workspace_id,
                    McpOAuthRegistration.server_id == normalized,
                    McpOAuthRegistration.issuer == issuer,
                )
            )
            if registration is None:
                registration = McpOAuthRegistration(
                    workspace_id=workspace_id,
                    server_id=normalized,
                    issuer=issuer,
                )
                session.add(registration)
            registration.resource = resource
            registration.registration_method = registration_method
            registration.client_id = client_id
            registration.endpoint_snapshot = endpoint_snapshot.model_dump()
            registration.metadata_fingerprint = metadata_fingerprint
            registration.client_metadata_fingerprint = client_metadata_fingerprint
            registration.scopes = sorted(set(scopes))
            await session.commit()
            await session.refresh(registration)
            return registration

    async def delete_for_server(self, workspace_id: str, server_id: str) -> None:
        normalized = self._server_uuid(server_id)
        if normalized is None:
            return
        async with self.async_session() as session:
            await session.execute(
                delete(McpOAuthRegistration).where(
                    McpOAuthRegistration.workspace_id == workspace_id,
                    McpOAuthRegistration.server_id == normalized,
                )
            )
            await session.commit()


oauth_registration_store = OAuthRegistrationStore(settings.DATABASE_URL)
