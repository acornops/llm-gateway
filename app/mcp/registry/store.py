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
from app.mcp.registry.models import Tool
from app.mcp.registry.server_store import McpServerRegistry
from app.mcp.registry.tool_registration import resolve_tool_registration
from app.outbound_tls import redis_tls_kwargs, sqlalchemy_connection_config

logger = structlog.get_logger()
TOOL_CACHE_INVALIDATION_CHANNEL = "gateway:tool-cache-invalidation"
ToolCacheKey = tuple[str, str, str, str, str, bool]


class ToolRegistry:
    """
    Tool registry with database persistence and in-memory caching.
    """

    def __init__(self, database_url: str):
        database_url, connect_args = sqlalchemy_connection_config(database_url)
        self.engine = create_async_engine(database_url, connect_args=connect_args)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._cache: dict[ToolCacheKey, tuple[Tool, float]] = {}
        self._cache_ttl = settings.TOOL_REGISTRY_CACHE_TTL_SEC
        self._redis = (
            Redis.from_url(settings.REDIS_URL, **redis_tls_kwargs(settings.REDIS_URL))
            if settings.REDIS_URL
            else None
        )
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None

    def _scope_cache_key(
        self,
        workspace_id: str,
        target_id: str,
        tool_name: str,
        target_type: str,
        server_id: str | None,
        include_disabled: bool,
    ) -> ToolCacheKey:
        return (
            workspace_id,
            target_id,
            target_type,
            server_id or "",
            tool_name,
            include_disabled,
        )

    @staticmethod
    def _scope_type(target_type: str) -> str:
        return "agent" if target_type == "agent" else "target"

    async def get_tool(
        self,
        workspace_id: str,
        target_id: str,
        tool_name: str,
        *,
        target_type: str,
        server_id: str | None = None,
        include_disabled: bool = False,
    ) -> Tool | None:
        """
        Retrieves a target-scoped tool from cache or database.
        """
        cache_key = self._scope_cache_key(
            workspace_id,
            target_id,
            tool_name,
            target_type,
            server_id,
            include_disabled,
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
            if server_id:
                try:
                    normalized_server_id = uuid.UUID(server_id)
                except (TypeError, ValueError, AttributeError):
                    return None
                stmt = stmt.where(Tool.server_id == normalized_server_id)
            if not include_disabled:
                stmt = stmt.where(Tool.enabled)
            result = await session.execute(stmt.limit(2))
            matches = list(result.scalars().all())
            tool = matches[0] if len(matches) == 1 else None

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
        output_schema: dict | None = None,
        artifact_policy: str = "never",
        enabled: bool = True,
        description: str | None = None,
        capability: str = "write",
        version: str = "v1",
        source: str = "mcp",
        server_id: str | None = None,
        review_state: str = "pending",
        risk_level: str = "high_risk",
        auto_allowed: bool = False,
    ) -> Tool:
        async with self.async_session() as session:
            server, existing = await resolve_tool_registration(
                session,
                tool_name=tool_name,
                mcp_server_url=mcp_server_url,
                workspace_id=workspace_id,
                scope_type=self._scope_type(target_type),
                target_id=target_id,
                target_type=target_type,
                server_id=server_id,
            )

            if existing:
                if existing.target_type != target_type:
                    raise ValueError(
                        f"tool_name '{tool_name}' is already registered for "
                        f"target_type={existing.target_type}; "
                        f"cannot update through target_type={target_type}"
                    )
                existing.server_id = server.id
                existing.timeout_ms = timeout_ms
                existing.target_type = target_type
                existing.input_schema = input_schema
                existing.output_schema = output_schema
                existing.artifact_policy = artifact_policy
                existing.enabled = enabled
                existing.description = description
                existing.capability = capability
                existing.version = version
                existing.source = source
                existing.agent_id = target_id if target_type == "agent" else None
                existing.review_state = review_state
                existing.risk_level = risk_level
                existing.auto_allowed = auto_allowed
                await session.commit()
                self._evict_scope_cache(
                    workspace_id, target_id, tool_name, target_type=target_type
                )
                await self._publish_scope_invalidation(
                    workspace_id, target_id, tool_name, target_type=target_type
                )
                return existing

            tool = Tool(
                server_id=server.id,
                workspace_id=workspace_id,
                scope_type=self._scope_type(target_type),
                agent_id=target_id if target_type == "agent" else None,
                target_id=target_id,
                target_type=target_type,
                tool_name=tool_name,
                mcp_server_url=mcp_server_url,
                enabled=enabled,
                input_schema=input_schema,
                output_schema=output_schema,
                artifact_policy=artifact_policy,
                timeout_ms=timeout_ms,
                description=description,
                capability=capability,
                version=version,
                source=source,
                review_state=review_state,
                risk_level=risk_level,
                auto_allowed=auto_allowed,
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
        server_id: str | None = None,
    ) -> bool:
        async with self.async_session() as session:
            stmt = select(Tool).where(
                Tool.workspace_id == workspace_id,
                Tool.scope_type == self._scope_type(target_type),
                Tool.target_id == target_id,
                Tool.target_type == target_type,
                Tool.tool_name == tool_name,
            )
            if server_id:
                try:
                    stmt = stmt.where(Tool.server_id == uuid.UUID(server_id))
                except (TypeError, ValueError, AttributeError):
                    return False
            result = await session.execute(stmt)
            tools = list(result.scalars().all())
            if len(tools) != 1:
                return False
            tool = tools[0]

            await session.execute(delete(Tool).where(Tool.id == tool.id))
            await session.commit()
            self._evict_scope_cache(
                workspace_id, target_id, tool_name, target_type=target_type
            )
            await self._publish_scope_invalidation(
                workspace_id, target_id, tool_name, target_type=target_type
            )
            return True

    async def remove_server_tools_not_in(
        self,
        workspace_id: str,
        target_id: str,
        target_type: str,
        server_id: str,
        tool_names: set[str],
    ) -> int:
        """Remove stale discovered tools after a successful, authoritative refresh."""
        try:
            normalized_server_id = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return 0
        async with self.async_session() as session:
            result = await session.execute(
                select(Tool).where(
                    Tool.workspace_id == workspace_id,
                    Tool.scope_type == self._scope_type(target_type),
                    Tool.target_id == target_id,
                    Tool.target_type == target_type,
                    Tool.server_id == normalized_server_id,
                    Tool.source == "mcp",
                )
            )
            stale = [tool for tool in result.scalars().all() if tool.tool_name not in tool_names]
            for tool in stale:
                await session.execute(delete(Tool).where(Tool.id == tool.id))
                self._evict_scope_cache(
                    workspace_id, target_id, tool.tool_name, target_type=target_type
                )
                await self._publish_scope_invalidation(
                    workspace_id, target_id, tool.tool_name, target_type=target_type
                )
            await session.commit()
            return len(stale)

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
            (
                key_workspace_id,
                key_target_id,
                key_target_type,
                _key_server_id,
                key_tool_name,
                _,
            ) = cache_key
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


tool_registry = ToolRegistry(settings.DATABASE_URL)
mcp_server_registry = McpServerRegistry(settings.DATABASE_URL)
