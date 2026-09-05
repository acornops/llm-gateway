import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.mcp.registry.models import McpServer, Tool
from app.outbound_tls import sqlalchemy_connection_config


class McpServerRegistry:
    """Registry for target- and Agent-scoped remote MCP server configurations."""

    @staticmethod
    def _normalize_server_id(server_id: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError):
            return None

    def __init__(self, database_url: str):
        database_url, connect_args = sqlalchemy_connection_config(database_url)
        self.engine = create_async_engine(database_url, connect_args=connect_args)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _scope_filters(model, scope_type: str, destination_id: str, target_type: str | None):
        if scope_type == "agent":
            return (
                model.scope_type == "agent",
                model.agent_id == destination_id,
                model.target_id.is_(None),
                model.target_type.is_(None),
            )
        if scope_type != "target" or not target_type:
            raise ValueError("target MCP scope requires target_type")
        return (
            model.scope_type == "target",
            model.agent_id.is_(None),
            model.target_id == destination_id,
            model.target_type == target_type,
        )

    async def list_servers(
        self,
        workspace_id: str,
        destination_id: str,
        target_type: str | None = None,
        *,
        scope_type: str = "target",
    ) -> list[McpServer]:
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer)
                .where(
                    McpServer.workspace_id == workspace_id,
                    *self._scope_filters(McpServer, scope_type, destination_id, target_type),
                )
                .order_by(McpServer.server_name.asc())
            )
            return list(result.scalars().all())

    async def list_workspace_servers(self, workspace_id: str) -> list[McpServer]:
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer)
                .where(McpServer.workspace_id == workspace_id)
                .order_by(McpServer.scope_type.asc(), McpServer.server_name.asc())
            )
            return list(result.scalars().all())

    async def get_server(
        self,
        workspace_id: str,
        destination_id: str,
        server_id: str,
        target_type: str | None = None,
        *,
        scope_type: str = "target",
    ) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    *self._scope_filters(McpServer, scope_type, destination_id, target_type),
                )
            )
            return result.scalars().first()

    async def get_server_for_workspace(self, workspace_id: str, server_id: str) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            return (
                (
                    await session.execute(
                        select(McpServer).where(
                            McpServer.id == normalized_server_id,
                            McpServer.workspace_id == workspace_id,
                        )
                    )
                )
                .scalars()
                .first()
            )

    async def get_server_by_url(
        self,
        workspace_id: str,
        destination_id: str,
        server_url: str,
        *,
        target_type: str | None = None,
        scope_type: str = "target",
        enabled_only: bool = True,
    ) -> McpServer | None:
        async with self.async_session() as session:
            stmt = select(McpServer).where(
                McpServer.workspace_id == workspace_id,
                *self._scope_filters(McpServer, scope_type, destination_id, target_type),
                McpServer.server_url == server_url,
            )
            if enabled_only:
                stmt = stmt.where(McpServer.enabled)
            return (await session.execute(stmt)).scalars().first()

    async def create_server(
        self,
        workspace_id: str,
        destination_id: str,
        server_name: str,
        server_url: str,
        enabled: bool,
        auth_type: str,
        target_type: str | None = None,
        auth_header_name: str | None = None,
        auth_header_prefix: str | None = None,
        public_headers: dict[str, str] | None = None,
        credential_mode: str = "none",
        catalog_source_id: str | None = None,
        catalog_artifact_name: str | None = None,
        catalog_version: str | None = None,
        catalog_digest: str | None = None,
        catalog_imported_at=None,
        endpoint_configuration: dict[str, str] | None = None,
        provenance_type: str = "manual",
        scope_type: str = "target",
    ) -> McpServer:
        async with self.async_session() as session:
            server = McpServer(
                workspace_id=workspace_id,
                scope_type=scope_type,
                agent_id=destination_id if scope_type == "agent" else None,
                target_id=destination_id if scope_type == "target" else None,
                target_type=target_type if scope_type == "target" else None,
                server_name=server_name,
                server_url=server_url,
                enabled=enabled,
                auth_type=auth_type,
                auth_header_name=auth_header_name,
                auth_header_prefix=auth_header_prefix,
                public_headers=public_headers,
                credential_mode=credential_mode,
                catalog_source_id=uuid.UUID(catalog_source_id) if catalog_source_id else None,
                catalog_artifact_name=catalog_artifact_name,
                catalog_version=catalog_version,
                catalog_digest=catalog_digest,
                catalog_imported_at=catalog_imported_at,
                provenance_type=provenance_type,
                endpoint_configuration=endpoint_configuration or {},
                revision=1,
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
        destination_id: str,
        server_id: str,
        patch: dict,
        target_type: str | None = None,
        scope_type: str = "target",
    ) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    *self._scope_filters(McpServer, scope_type, destination_id, target_type),
                )
            )
            server = result.scalars().first()
            if not server:
                return None
            expected_revision = patch.pop("expected_revision", None)
            if expected_revision is not None and server.revision != expected_revision:
                raise ValueError("MCP server revision does not match")
            previous_server_url = server.server_url
            for key, value in patch.items():
                setattr(server, key, value)
            if server.server_url != previous_server_url:
                await session.execute(
                    update(Tool)
                    .where(Tool.server_id == server.id)
                    .values(mcp_server_url=server.server_url)
                )
            server.revision = int(server.revision or 1) + 1
            await session.commit()
            await session.refresh(server)
            return server

    async def sync_builtin_server(
        self,
        *,
        workspace_id: str,
        destination_id: str,
        server_id: str | None,
        server_name: str,
        server_url: str,
        enabled: bool,
        tools: list[dict],
        target_type: str | None = None,
        scope_type: str = "target",
        increment_credential_epoch: bool = False,
    ) -> McpServer:
        """Atomically reconcile one built-in server and its authoritative tools.

        Existing server and tool enablement is read under the database row lock
        and preserved. Synchronizers own definitions; administrators own the
        current enablement decision.
        """

        normalized_requested_id = self._normalize_server_id(server_id) if server_id else None
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer)
                .where(
                    McpServer.workspace_id == workspace_id,
                    *self._scope_filters(McpServer, scope_type, destination_id, target_type),
                    McpServer.provenance_type == "builtin",
                )
                .with_for_update()
            )
            matches = list(result.scalars().all())
            if len(matches) > 1:
                raise ValueError("MCP_DUPLICATE_BUILTIN_SERVER_ANOMALY")
            server = matches[0] if matches else None
            if server_id is not None and (
                server is None
                or normalized_requested_id is None
                or server.id != normalized_requested_id
            ):
                raise ValueError("Built-in MCP server identity changed")

            if server is None:
                server = McpServer(
                    workspace_id=workspace_id,
                    scope_type=scope_type,
                    agent_id=destination_id if scope_type == "agent" else None,
                    target_id=destination_id if scope_type == "target" else None,
                    target_type=target_type if scope_type == "target" else None,
                    server_name=server_name,
                    server_url=server_url,
                    enabled=enabled,
                    auth_type="none",
                    auth_header_name=None,
                    auth_header_prefix=None,
                    public_headers={},
                    credential_mode="none",
                    credential_transitioning=False,
                    credential_epoch=1,
                    provenance_type="builtin",
                    endpoint_configuration={},
                    revision=1,
                    connection_status="unknown",
                    last_discovery_at=None,
                    last_discovery_error=None,
                )
                session.add(server)
                await session.flush()
            else:
                server.server_name = server_name
                server.server_url = server_url
                server.auth_type = "none"
                server.credential_mode = "none"
                server.auth_header_name = None
                server.auth_header_prefix = None
                server.public_headers = {}
                server.credential_transitioning = False
                if increment_credential_epoch:
                    server.credential_epoch = int(server.credential_epoch or 1) + 1
                server.connection_status = "unknown"
                server.last_discovery_at = None
                server.last_discovery_error = None
                server.revision = int(server.revision or 1) + 1

            existing_tools = list(
                (
                    await session.execute(
                        select(Tool).where(Tool.server_id == server.id).with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            existing_by_name = {tool.tool_name: tool for tool in existing_tools}
            requested_names = {str(tool["name"]) for tool in tools}
            for stale in existing_tools:
                if stale.tool_name not in requested_names:
                    await session.delete(stale)

            for definition in tools:
                tool_name = str(definition["name"])
                tool = existing_by_name.get(tool_name)
                if tool is None:
                    tool = Tool(
                        server_id=server.id,
                        workspace_id=workspace_id,
                        scope_type=scope_type,
                        agent_id=destination_id if scope_type == "agent" else None,
                        target_id=destination_id if scope_type == "target" else None,
                        target_type=target_type if scope_type == "target" else None,
                        tool_name=tool_name,
                        enabled=bool(definition["enabled"]),
                    )
                    session.add(tool)
                tool.mcp_server_url = server_url
                tool.timeout_ms = definition["timeout_ms"]
                tool.input_schema = definition.get("input_schema")
                tool.output_schema = definition.get("output_schema")
                tool.artifact_policy = definition.get("artifact_policy", "never")
                tool.description = definition.get("description")
                tool.capability = definition.get("capability", "write")
                tool.version = definition.get("version", "v1")
                tool.source = "builtin"
                tool.review_state = definition.get("review_state", "approved")
                tool.risk_level = definition.get("risk_level", "high_risk")
                tool.auto_allowed = bool(definition.get("auto_allowed", False))

            await session.commit()
            await session.refresh(server)
            return server

    async def delete_server(
        self,
        workspace_id: str,
        destination_id: str,
        server_id: str,
        target_type: str | None = None,
        *,
        scope_type: str = "target",
    ) -> bool:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return False
        async with self.async_session() as session:
            result = await session.execute(
                delete(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    *self._scope_filters(McpServer, scope_type, destination_id, target_type),
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def close(self):
        await self.engine.dispose()
