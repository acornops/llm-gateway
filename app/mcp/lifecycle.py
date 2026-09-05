"""Durable MCP lifecycle fencing and cross-replica mutation serialization."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import settings
from app.mcp.identity import canonical_mcp_server_id
from app.mcp.lifecycle_types import (
    McpCredentialTransitioningError,
    McpDestination,
    McpLifecycleEpochChangedError,
    McpLifecycleFencedError,
    McpLifecycleServerNotFoundError,
    McpUserLifecycleConflictError,
    McpUserLifecycleStaleError,
)
from app.mcp.registry.models import McpLifecycleFence, McpServer, McpUserLifecycle
from app.mcp.user_lifecycle_contract import MAX_MCP_MEMBERSHIP_GENERATION
from app.outbound_tls import sqlalchemy_connection_config

_ADVISORY_LOCK_POOL_SIZE = 5
_USER_LIFECYCLE_LOCK_POOL_SIZE = 2

class McpLifecycleStore:
    """Coordinates terminal teardown with ordinary MCP mutations.

    Ordinary lock order is workspace, destination, server, then connection
    owner. Membership reconciliation adds an outer (workspace, user) lifecycle
    lock spanning stage/sweep/finalize; it never nests beneath a server lock.
    The connection-owner lock remains owned by ``McpConnectionStore``.
    PostgreSQL advisory locks provide the cross-replica layer in PostgreSQL;
    local locks keep SQLite and unit tests deterministic.
    """

    def __init__(self, database_url: str) -> None:
        database_url, connect_args = sqlalchemy_connection_config(database_url)
        self.engine = create_async_engine(database_url, connect_args=connect_args)
        self._supports_advisory_locks = self.engine.dialect.name == "postgresql"
        # Advisory-lock holders must never compete with state/query sessions for
        # the same finite pool. Otherwise enough concurrent operations can each
        # pin a lock connection and deadlock waiting for a second data connection.
        self._advisory_lock_engine = (
            create_async_engine(
                database_url,
                connect_args=connect_args,
                pool_size=_ADVISORY_LOCK_POOL_SIZE,
                max_overflow=0,
            )
            if self._supports_advisory_locks
            else None
        )
        # Long-lived owner lifecycle serialization must not consume the normal
        # lifecycle query/short-lock pool. A separate bounded pool prevents a
        # batch of secret sweeps from starving the commits needed to finish them.
        self._session_lock_engine = (
            create_async_engine(
                database_url,
                connect_args=connect_args,
                pool_size=_USER_LIFECYCLE_LOCK_POOL_SIZE,
                max_overflow=0,
            )
            if self._supports_advisory_locks
            else None
        )
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._locks: dict[tuple[str, ...], tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()
        self._advisory_connection: ContextVar[AsyncConnection | None] = ContextVar(
            f"mcp_lifecycle_advisory_connection_{id(self)}",
            default=None,
        )

    async def close(self) -> None:
        if self._session_lock_engine is not None:
            await self._session_lock_engine.dispose()
        if self._advisory_lock_engine is not None:
            await self._advisory_lock_engine.dispose()
        await self.engine.dispose()

    @staticmethod
    def _advisory_key(key: tuple[str, ...]) -> int:
        material = json.dumps(key, separators=(",", ":")).encode()
        return int.from_bytes(
            hashlib.blake2b(material, digest_size=8).digest(),
            byteorder="big",
            signed=True,
        )

    @asynccontextmanager
    async def _local_lock(self, key: tuple[str, ...]) -> AsyncIterator[None]:
        async with self._locks_guard:
            lock, users = self._locks.get(key, (asyncio.Lock(), 0))
            self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            async with self._locks_guard:
                current = self._locks.get(key)
                if current is not None and current[0] is lock:
                    remaining = current[1] - 1
                    if remaining == 0:
                        self._locks.pop(key, None)
                    else:
                        self._locks[key] = (lock, remaining)

    @asynccontextmanager
    async def _mutation_lock(self, key: tuple[str, ...]) -> AsyncIterator[None]:
        async with self._local_lock(key):
            if not self._supports_advisory_locks:
                yield
                return
            existing_connection = self._advisory_connection.get()
            if existing_connection is not None:
                await existing_connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": self._advisory_key(key)},
                )
                yield
                return
            assert self._advisory_lock_engine is not None
            async with (
                self._advisory_lock_engine.connect() as connection,
                connection.begin(),
            ):
                token = self._advisory_connection.set(connection)
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": self._advisory_key(key)},
                    )
                    yield
                finally:
                    self._advisory_connection.reset(token)

    def workspace_mutation_lock(self, workspace_id: str):
        return self._mutation_lock(("workspace", workspace_id))

    def destination_mutation_lock(self, destination: McpDestination):
        return self._mutation_lock(
            (
                "destination",
                destination.workspace_id,
                destination.scope_type,
                destination.target_type or "",
                destination.destination_id,
            )
        )

    def server_mutation_lock(self, workspace_id: str, server_id: str):
        return self._mutation_lock(
            ("server", workspace_id, canonical_mcp_server_id(server_id))
        )

    @asynccontextmanager
    async def user_lifecycle_operation(
        self,
        workspace_id: str,
        user_id: str,
    ) -> AsyncIterator[None]:
        """Serialize one membership generation through stage, sweep, and finalize.

        The cross-replica user lock is session-scoped and uses a dedicated pool,
        so the handler may commit removed/activating state before external secret
        cleanup without holding the workspace lock or starving normal MCP work.
        """

        key = ("user-lifecycle", workspace_id, user_id)
        async with self._local_lock(key):
            if not self._supports_advisory_locks:
                yield
                return
            assert self._session_lock_engine is not None
            connection = await self._session_lock_engine.connect()

            async def cleanup() -> None:
                try:
                    await connection.execute(text("SELECT pg_advisory_unlock_all()"))
                    await connection.commit()
                except BaseException:
                    with suppress(BaseException):
                        await connection.invalidate()
                    raise
                finally:
                    with suppress(BaseException):
                        await connection.close()

            try:
                await connection.execute(
                    text("SELECT pg_advisory_lock(:lock_key)"),
                    {"lock_key": self._advisory_key(key)},
                )
                await connection.commit()
                yield
            finally:
                cleanup_task = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    with suppress(BaseException):
                        await cleanup_task
                    raise

    async def _get_server(
        self,
        workspace_id: str,
        server_id: str,
        *,
        connection: AsyncConnection | None = None,
    ) -> McpServer | None:
        try:
            import uuid

            normalized = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None
        session_context = (
            self.async_session()
            if connection is None
            else AsyncSession(bind=connection, expire_on_commit=False)
        )
        async with session_context as session:
            return (
                await session.execute(
                    select(McpServer).where(
                        McpServer.workspace_id == workspace_id,
                        McpServer.id == normalized,
                    )
                )
            ).scalars().first()

    async def is_fenced(
        self,
        destination: McpDestination,
        *,
        connection: AsyncConnection | None = None,
    ) -> bool:
        session_context = (
            self.async_session()
            if connection is None
            else AsyncSession(bind=connection, expire_on_commit=False)
        )
        async with session_context as session:
            return (
                await session.execute(
                    select(McpLifecycleFence.fence_key).where(
                        McpLifecycleFence.workspace_id == destination.workspace_id,
                        McpLifecycleFence.fence_key.in_(("workspace", destination.fence_key)),
                    )
                )
            ).first() is not None

    async def assert_not_fenced(
        self,
        destination: McpDestination,
        *,
        connection: AsyncConnection | None = None,
    ) -> None:
        if await self.is_fenced(destination, connection=connection):
            raise McpLifecycleFencedError("MCP destination is fenced for teardown")

    async def assert_workspace_not_fenced(self, workspace_id: str) -> None:
        async with self.async_session() as session:
            fenced = await session.get(McpLifecycleFence, (workspace_id, "workspace"))
        if fenced is not None:
            raise McpLifecycleFencedError("MCP workspace is fenced for teardown")

    async def _activate_fence(
        self,
        *,
        workspace_id: str,
        fence_key: str,
        scope_type: str,
        destination_id: str | None,
        target_type: str | None,
    ) -> McpLifecycleFence:
        async with self.async_session() as session:
            existing = await session.get(McpLifecycleFence, (workspace_id, fence_key))
            if existing is not None:
                return existing
            fence = McpLifecycleFence(
                workspace_id=workspace_id,
                fence_key=fence_key,
                scope_type=scope_type,
                destination_id=destination_id,
                target_type=target_type,
                epoch=1,
            )
            session.add(fence)
            await session.commit()
            await session.refresh(fence)
            return fence

    async def activate_workspace_fence(self, workspace_id: str) -> McpLifecycleFence:
        return await self._activate_fence(
            workspace_id=workspace_id,
            fence_key="workspace",
            scope_type="workspace",
            destination_id=None,
            target_type=None,
        )

    async def activate_destination_fence(
        self, destination: McpDestination
    ) -> McpLifecycleFence:
        return await self._activate_fence(
            workspace_id=destination.workspace_id,
            fence_key=destination.fence_key,
            scope_type=destination.scope_type,
            destination_id=destination.destination_id,
            target_type=destination.target_type,
        )

    async def reconcile_user_lifecycle(
        self,
        workspace_id: str,
        user_id: str,
        membership_generation: int,
        status: str,
    ) -> str:
        """Apply trusted membership state while the caller holds the workspace lock.

        Returns ``applied``, ``idempotent``, or ``activation_required``. Active
        state is staged durably as ``activating`` so cleanup failure never opens
        a window in which residual credentials can be used. A lower generation
        is stale and an equal generation with different external state is a
        control-plane contract conflict.
        """

        if (
            membership_generation <= 0
            or membership_generation > MAX_MCP_MEMBERSHIP_GENERATION
            or status not in ("active", "removed")
        ):
            raise ValueError("invalid MCP user lifecycle state")
        async with self.async_session() as session:
            current = (
                await session.execute(
                    select(McpUserLifecycle)
                    .where(
                        McpUserLifecycle.workspace_id == workspace_id,
                        McpUserLifecycle.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            if current is None:
                session.add(
                    McpUserLifecycle(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        membership_generation=membership_generation,
                        status="activating" if status == "active" else "removed",
                    )
                )
                await session.commit()
                return "activation_required" if status == "active" else "applied"
            current_generation = int(current.membership_generation)
            if membership_generation < current_generation:
                raise McpUserLifecycleStaleError(
                    "workspace membership generation is older than lifecycle state"
                )
            if membership_generation == current_generation:
                if status == "active" and current.status == "activating":
                    return "activation_required"
                if current.status != status:
                    raise McpUserLifecycleConflictError(
                        "workspace membership generation has conflicting lifecycle state"
                    )
                return "idempotent"
            current.membership_generation = membership_generation
            current.status = "activating" if status == "active" else "removed"
            await session.commit()
            return "activation_required" if status == "active" else "applied"

    async def complete_user_activation(
        self,
        workspace_id: str,
        user_id: str,
        membership_generation: int,
    ) -> None:
        """Commit active state after old-generation credentials are drained."""

        async with self.async_session() as session:
            current = (
                await session.execute(
                    select(McpUserLifecycle)
                    .where(
                        McpUserLifecycle.workspace_id == workspace_id,
                        McpUserLifecycle.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            if (
                current is None
                or int(current.membership_generation) != membership_generation
                or current.status != "activating"
            ):
                raise McpUserLifecycleConflictError(
                    "workspace membership activation state changed during cleanup"
                )
            current.status = "active"
            await session.commit()

    async def assert_user_active(
        self,
        workspace_id: str,
        user_id: str,
        membership_generation: int | None,
    ) -> McpUserLifecycle | None:
        async with self.async_session() as session:
            current = await session.get(McpUserLifecycle, (workspace_id, user_id))
        if membership_generation is None:
            raise McpUserLifecycleStaleError(
                "workspace membership generation is required for this owner"
            )
        if not 1 <= membership_generation <= MAX_MCP_MEMBERSHIP_GENERATION:
            raise McpUserLifecycleStaleError(
                "workspace membership generation is invalid"
            )
        if (
            current is None
            or current.status != "active"
            or int(current.membership_generation) != membership_generation
        ):
            raise McpUserLifecycleStaleError(
                "workspace membership generation is no longer active"
            )
        return current

    async def delete_user_lifecycles_for_workspace(self, workspace_id: str) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                delete(McpUserLifecycle).where(
                    McpUserLifecycle.workspace_id == workspace_id
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    @asynccontextmanager
    async def destination_operation(
        self, destination: McpDestination
    ) -> AsyncIterator[None]:
        """Hold the scope locks for a create or authoritative reconciliation."""

        async with (
            self.workspace_mutation_lock(destination.workspace_id),
            self.destination_mutation_lock(destination),
        ):
            await self.assert_not_fenced(destination)
            yield

    @asynccontextmanager
    async def server_operation(
        self,
        workspace_id: str,
        server_id: str,
        *,
        expected_credential_epoch: int | None = None,
        allow_transitioning: bool = False,
    ) -> AsyncIterator[McpServer]:
        """Fence-check and serialize one server without holding broad locks remotely.

        Workspace and destination locks are handed off after the server lock is
        acquired. A teardown that starts afterward commits its tombstone and
        then waits for this server operation before its final cleanup sweep.
        """

        server_id = canonical_mcp_server_id(server_id)
        initial = await self._get_server(workspace_id, server_id)
        if initial is None:
            raise McpLifecycleServerNotFoundError("MCP server not found")
        destination = McpDestination.from_server(initial)
        workspace_key = ("workspace", workspace_id)
        destination_key = (
            "destination",
            destination.workspace_id,
            destination.scope_type,
            destination.target_type or "",
            destination.destination_id,
        )
        server_key = ("server", workspace_id, server_id)
        workspace_lock = self._local_lock(workspace_key)
        destination_lock = self._local_lock(destination_key)
        server_lock = self._local_lock(server_key)
        workspace_entered = destination_entered = server_entered = False
        advisory_connection: AsyncConnection | None = None
        advisory_keys: list[tuple[str, ...]] = []

        async def acquire_advisory(key: tuple[str, ...]) -> None:
            if advisory_connection is None:
                return
            await advisory_connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": self._advisory_key(key)},
            )
            advisory_keys.append(key)

        async def release_advisory(key: tuple[str, ...]) -> None:
            if advisory_connection is None or key not in advisory_keys:
                return
            await advisory_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": self._advisory_key(key)},
            )
            advisory_keys.remove(key)

        async def close_advisory_connection() -> None:
            if advisory_connection is None:
                return
            try:
                # Session locks survive transaction rollback and pool reset.
                # Release every lock owned by this dedicated handoff connection
                # before it can return to the pool.
                await advisory_connection.execute(
                    text("SELECT pg_advisory_unlock_all()")
                )
                advisory_keys.clear()
            except BaseException:
                # Never return a possibly lock-poisoned physical connection to
                # the pool. Invalidation closes it and PostgreSQL releases the
                # session locks server-side.
                with suppress(BaseException):
                    await advisory_connection.invalidate()
                raise
            finally:
                with suppress(BaseException):
                    await advisory_connection.close()

        try:
            await workspace_lock.__aenter__()
            workspace_entered = True
            if self._supports_advisory_locks:
                assert self._advisory_lock_engine is not None
                advisory_connection = await self._advisory_lock_engine.connect()
                await acquire_advisory(workspace_key)
            await destination_lock.__aenter__()
            destination_entered = True
            await acquire_advisory(destination_key)
            await self.assert_not_fenced(
                destination,
                connection=advisory_connection,
            )
            await server_lock.__aenter__()
            server_entered = True
            await acquire_advisory(server_key)
            current = await self._get_server(
                workspace_id,
                server_id,
                connection=advisory_connection,
            )
            if current is None:
                raise McpLifecycleServerNotFoundError("MCP server not found")
            if McpDestination.from_server(current) != destination:
                raise McpLifecycleEpochChangedError("MCP server destination changed")
            await self.assert_not_fenced(
                destination,
                connection=advisory_connection,
            )
            current_epoch = int(getattr(current, "credential_epoch", 1) or 1)
            if (
                expected_credential_epoch is not None
                and current_epoch != expected_credential_epoch
            ):
                raise McpLifecycleEpochChangedError("MCP credential epoch changed")
            if getattr(current, "credential_transitioning", False) and not allow_transitioning:
                raise McpCredentialTransitioningError(
                    "MCP credential transition is in progress"
                )
            await release_advisory(destination_key)
            await destination_lock.__aexit__(None, None, None)
            destination_entered = False
            await release_advisory(workspace_key)
            await workspace_lock.__aexit__(None, None, None)
            workspace_entered = False
            yield current
        finally:
            try:
                if advisory_connection is not None:
                    cleanup_task = asyncio.create_task(close_advisory_connection())
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        with suppress(BaseException):
                            await cleanup_task
                        raise
            finally:
                if server_entered:
                    await server_lock.__aexit__(None, None, None)
                if destination_entered:
                    await destination_lock.__aexit__(None, None, None)
                if workspace_entered:
                    await workspace_lock.__aexit__(None, None, None)

    async def assert_server_active(
        self,
        workspace_id: str,
        server_id: str,
        *,
        expected_credential_epoch: int | None = None,
    ) -> McpServer:
        """Perform a bounded start-time fence and epoch check.

        Runtime dispatch deliberately releases the lifecycle lock before the
        remote call. Calls that have passed this check may finish while a later
        teardown waits only for local credential snapshot/persistence work.
        """

        async with self.server_operation(
            workspace_id,
            server_id,
            expected_credential_epoch=expected_credential_epoch,
        ) as server:
            return server


mcp_lifecycle_store = McpLifecycleStore(settings.DATABASE_URL)
