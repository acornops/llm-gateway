import asyncio
import contextlib
import json
import time
import uuid

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.mcp.registry.models import McpServer, Tool

logger = structlog.get_logger()
TOOL_CACHE_INVALIDATION_CHANNEL = "gateway:tool-cache-invalidation"
ToolCacheKey = tuple[str, str, str, str, bool]


class ToolRegistry:
    """
    Tool registry with database persistence and in-memory caching.
    """

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._cache: dict[ToolCacheKey, tuple[Tool, float]] = {}
        self._cache_ttl = settings.TOOL_REGISTRY_CACHE_TTL_SEC
        self._redis = Redis.from_url(settings.REDIS_URL) if settings.REDIS_URL else None
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None

    def _scope_cache_key(
        self,
        workspace_id: str,
        target_id: str,
        tool_name: str,
        target_type: str,
        include_disabled: bool,
    ) -> ToolCacheKey:
        return (workspace_id, target_id, target_type, tool_name, include_disabled)

    @staticmethod
    def _scope_type(target_type: str) -> str:
        return "workspace" if target_type == "workspace" else "target"

    async def get_tool(
        self,
        workspace_id: str,
        target_id: str,
        tool_name: str,
        *,
        target_type: str,
        include_disabled: bool = False,
    ) -> Tool | None:
        """
        Retrieves a target-scoped tool from cache or database.
        """
        cache_key = self._scope_cache_key(
            workspace_id, target_id, tool_name, target_type, include_disabled
        )
        if cache_key in self._cache:
            tool, expires_at = self._cache[cache_key]
            if time.time() < expires_at:
                return tool

        async with self.async_session() as session:
            stmt = select(Tool).where(
                Tool.workspace_id == workspace_id,
                Tool.scope_type == self._scope_type(target_type),
                Tool.target_id == target_id,
                Tool.target_type == target_type,
                Tool.tool_name == tool_name,
            )
            if not include_disabled:
                stmt = stmt.where(Tool.enabled)
            result = await session.execute(stmt)
            tool = result.scalars().first()

            if tool:
                self._cache[cache_key] = (tool, time.time() + self._cache_ttl)

            return tool

    async def list_target_tools(
        self,
        workspace_id: str,
        target_id: str,
        *,
        target_type: str,
        include_disabled: bool = False,
    ) -> list[Tool]:
        async with self.async_session() as session:
            stmt = select(Tool).where(
                Tool.workspace_id == workspace_id,
                Tool.scope_type == self._scope_type(target_type),
                Tool.target_id == target_id,
                Tool.target_type == target_type,
            )
            if not include_disabled:
                stmt = stmt.where(Tool.enabled)
            stmt = stmt.order_by(Tool.tool_name.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_target_tool_names(
        self, workspace_id: str, target_id: str, target_type: str
    ) -> list[str]:
        tools = await self.list_target_tools(
            workspace_id, target_id, target_type=target_type
        )
        return sorted({tool.tool_name for tool in tools})

    async def upsert_tool(
        self,
        tool_name: str,
        mcp_server_url: str,
        workspace_id: str,
        target_id: str,
        target_type: str,
        timeout_ms: int = 10000,
        input_schema: dict | None = None,
        enabled: bool = True,
        description: str | None = None,
        capability: str = "write",
        version: str = "v1",
        source: str = "mcp",
    ) -> Tool:
        async with self.async_session() as session:
            stmt = select(Tool).where(
                Tool.workspace_id == workspace_id,
                Tool.scope_type == self._scope_type(target_type),
                Tool.target_id == target_id,
                Tool.tool_name == tool_name,
            )
            result = await session.execute(stmt)
            existing = result.scalars().first()

            if existing:
                if existing.target_type != target_type:
                    raise ValueError(
                        f"tool_name '{tool_name}' is already registered for "
                        f"target_type={existing.target_type}; "
                        f"cannot update through target_type={target_type}"
                    )
                if existing.mcp_server_url != mcp_server_url:
                    raise ValueError(
                        f"tool_name '{tool_name}' is already bound to {existing.mcp_server_url}; "
                        f"cannot rebind to {mcp_server_url}"
                    )
                existing.timeout_ms = timeout_ms
                existing.target_type = target_type
                existing.input_schema = input_schema
                existing.enabled = enabled
                existing.description = description
                existing.capability = capability
                existing.version = version
                existing.source = source
                await session.commit()
                self._evict_scope_cache(
                    workspace_id, target_id, tool_name, target_type=target_type
                )
                await self._publish_scope_invalidation(
                    workspace_id, target_id, tool_name, target_type=target_type
                )
                return existing

            tool = Tool(
                workspace_id=workspace_id,
                scope_type=self._scope_type(target_type),
                target_id=target_id,
                target_type=target_type,
                tool_name=tool_name,
                mcp_server_url=mcp_server_url,
                enabled=enabled,
                input_schema=input_schema,
                timeout_ms=timeout_ms,
                description=description,
                capability=capability,
                version=version,
                source=source,
            )
            session.add(tool)
            await session.commit()
            self._evict_scope_cache(
                workspace_id, target_id, tool_name, target_type=target_type
            )
            await self._publish_scope_invalidation(
                workspace_id, target_id, tool_name, target_type=target_type
            )
            return tool

    async def remove_tool_for_target(
        self,
        tool_name: str,
        workspace_id: str,
        target_id: str,
        target_type: str,
    ) -> bool:
        async with self.async_session() as session:
            stmt = select(Tool).where(
                Tool.workspace_id == workspace_id,
                Tool.scope_type == self._scope_type(target_type),
                Tool.target_id == target_id,
                Tool.target_type == target_type,
                Tool.tool_name == tool_name,
            )
            result = await session.execute(stmt)
            tool = result.scalars().first()
            if not tool:
                return False

            await session.execute(delete(Tool).where(Tool.id == tool.id))
            await session.commit()
            self._evict_scope_cache(
                workspace_id, target_id, tool_name, target_type=target_type
            )
            await self._publish_scope_invalidation(
                workspace_id, target_id, tool_name, target_type=target_type
            )
            return True

    async def delete_target_tools_by_source(
        self, workspace_id: str, target_id: str, target_type: str, source: str
    ) -> None:
        async with self.async_session() as session:
            stmt = select(Tool).where(
                Tool.workspace_id == workspace_id,
                Tool.scope_type == self._scope_type(target_type),
                Tool.target_id == target_id,
                Tool.target_type == target_type,
                Tool.source == source,
            )
            result = await session.execute(stmt)
            tools = list(result.scalars().all())
            for tool in tools:
                await session.execute(delete(Tool).where(Tool.id == tool.id))
                self._evict_scope_cache(
                    workspace_id, target_id, tool.tool_name, target_type=tool.target_type
                )
                await self._publish_scope_invalidation(
                    workspace_id, target_id, tool.tool_name, target_type=tool.target_type
                )
            await session.commit()

    def _evict_scope_cache(
        self,
        workspace_id: str,
        target_id: str,
        tool_name: str,
        target_type: str | None = None,
    ) -> None:
        for cache_key in list(self._cache):
            key_workspace_id, key_target_id, key_target_type, key_tool_name, _ = cache_key
            if (
                key_workspace_id == workspace_id
                and key_target_id == target_id
                and key_tool_name == tool_name
                and (target_type is None or key_target_type == target_type)
            ):
                self._cache.pop(cache_key, None)

    async def _publish_scope_invalidation(
        self,
        workspace_id: str,
        target_id: str,
        tool_name: str,
        target_type: str | None = None,
    ) -> None:
        if self._redis is None:
            return
        payload = json.dumps(
            {
                "workspace_id": workspace_id,
                "target_id": target_id,
                "target_type": target_type,
                "tool_name": tool_name,
            }
        )
        try:
            await self._redis.publish(TOOL_CACHE_INVALIDATION_CHANNEL, payload)
        except Exception as exc:
            logger.warning(
                "tool_cache_invalidation_publish_failed",
                workspace_id=workspace_id,
                target_id=target_id,
                tool_name=tool_name,
                error=str(exc),
            )

    async def start_cache_invalidation_listener(self) -> None:
        if self._redis is None or self._listener_task is not None:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(TOOL_CACHE_INVALIDATION_CHANNEL)
        self._listener_task = asyncio.create_task(self._listen_for_invalidations())

    async def _listen_for_invalidations(self) -> None:
        if self._pubsub is None:
            return
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                payload = json.loads(data)
                self._evict_scope_cache(
                    payload["workspace_id"],
                    payload["target_id"],
                    payload["tool_name"],
                    target_type=payload.get("target_type"),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("tool_cache_invalidation_listener_failed", error=str(exc))

    async def close(self):
        """Closes the underlying database engine."""
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        if self._pubsub is not None:
            await self._pubsub.close()
            self._pubsub = None
        if self._redis is not None:
            await self._redis.aclose()
        await self.engine.dispose()

    async def register_tool(self, tool: Tool) -> None:
        async with self.async_session() as session:
            session.add(tool)
            await session.commit()
            self._evict_scope_cache(
                tool.workspace_id,
                tool.target_id,
                tool.tool_name,
                target_type=tool.target_type,
            )
            await self._publish_scope_invalidation(
                tool.workspace_id,
                tool.target_id,
                tool.tool_name,
                target_type=tool.target_type,
            )


class McpServerRegistry:
    """
    Registry for target-scoped remote MCP server configurations.
    """

    @staticmethod
    def _normalize_server_id(server_id: str) -> uuid.UUID | None:
        """Converts a string server ID to UUID and returns None for invalid input."""
        try:
            return uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _scope_type(target_type: str) -> str:
        return "workspace" if target_type == "workspace" else "target"

    async def list_servers(
        self,
        workspace_id: str,
        target_id: str,
        target_type: str,
    ) -> list[McpServer]:
        async with self.async_session() as session:
            stmt = (
                select(McpServer)
                .where(
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == self._scope_type(target_type),
                    McpServer.target_id == target_id,
                    McpServer.target_type == target_type,
                )
                .order_by(McpServer.server_name.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_server(
        self,
        workspace_id: str,
        target_id: str,
        server_id: str,
        target_type: str,
    ) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            stmt = select(McpServer).where(
                McpServer.id == normalized_server_id,
                McpServer.workspace_id == workspace_id,
                McpServer.scope_type == self._scope_type(target_type),
                McpServer.target_id == target_id,
                McpServer.target_type == target_type,
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_server_by_url(
        self,
        workspace_id: str,
        target_id: str,
        server_url: str,
        *,
        target_type: str,
        enabled_only: bool = True,
    ) -> McpServer | None:
        async with self.async_session() as session:
            stmt = select(McpServer).where(
                McpServer.workspace_id == workspace_id,
                McpServer.scope_type == self._scope_type(target_type),
                McpServer.target_id == target_id,
                McpServer.target_type == target_type,
                McpServer.server_url == server_url,
            )
            if enabled_only:
                stmt = stmt.where(McpServer.enabled)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def create_server(
        self,
        workspace_id: str,
        target_id: str,
        target_type: str,
        server_name: str,
        server_url: str,
        enabled: bool,
        auth_type: str,
        auth_secret_name: str | None = None,
        auth_header_name: str | None = None,
        auth_header_prefix: str | None = None,
        public_headers: dict[str, str] | None = None,
    ) -> McpServer:
        async with self.async_session() as session:
            server = McpServer(
                workspace_id=workspace_id,
                scope_type=self._scope_type(target_type),
                target_id=target_id,
                target_type=target_type,
                server_name=server_name,
                server_url=server_url,
                enabled=enabled,
                auth_type=auth_type,
                auth_secret_name=auth_secret_name,
                auth_header_name=auth_header_name,
                auth_header_prefix=auth_header_prefix,
                public_headers=public_headers,
                connection_status="unknown",
                last_discovery_at=None,
                last_discovery_error=None,
            )
            session.add(server)
            await session.commit()
            await session.refresh(server)
            return server

    async def update_server(
        self,
        workspace_id: str,
        target_id: str,
        server_id: str,
        patch: dict,
        target_type: str,
    ) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            stmt = select(McpServer).where(
                McpServer.id == normalized_server_id,
                McpServer.workspace_id == workspace_id,
                McpServer.scope_type == self._scope_type(target_type),
                McpServer.target_id == target_id,
                McpServer.target_type == target_type,
            )
            result = await session.execute(stmt)
            server = result.scalars().first()
            if not server:
                return None

            for key, value in patch.items():
                setattr(server, key, value)

            await session.commit()
            await session.refresh(server)
            return server

    async def delete_server(
        self,
        workspace_id: str,
        target_id: str,
        server_id: str,
        target_type: str,
    ) -> bool:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return False
        async with self.async_session() as session:
            stmt = delete(McpServer).where(
                McpServer.id == normalized_server_id,
                McpServer.workspace_id == workspace_id,
                McpServer.scope_type == self._scope_type(target_type),
                McpServer.target_id == target_id,
                McpServer.target_type == target_type,
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def close(self):
        await self.engine.dispose()


tool_registry = ToolRegistry(settings.DATABASE_URL)
mcp_server_registry = McpServerRegistry(settings.DATABASE_URL)
