import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.mcp.registry.models import McpServer
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
    def _scope_type(target_type: str) -> str:
        return "agent" if target_type == "agent" else "target"

    async def list_servers(
        self, workspace_id: str, target_id: str, target_type: str
    ) -> list[McpServer]:
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer)
                .where(
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == self._scope_type(target_type),
                    McpServer.target_id == target_id,
                    McpServer.target_type == target_type,
                )
                .order_by(McpServer.server_name.asc())
            )
            return list(result.scalars().all())

    async def get_server(
        self, workspace_id: str, target_id: str, server_id: str, target_type: str
    ) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == self._scope_type(target_type),
                    McpServer.target_id == target_id,
                    McpServer.target_type == target_type,
                )
            )
            return result.scalars().first()

    async def get_server_for_workspace(self, workspace_id: str, server_id: str) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            return (
                await session.execute(
                    select(McpServer).where(
                        McpServer.id == normalized_server_id,
                        McpServer.workspace_id == workspace_id,
                    )
                )
            ).scalars().first()

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
            return (await session.execute(stmt)).scalars().first()

    async def create_server(
        self,
        workspace_id: str,
        target_id: str,
        target_type: str,
        server_name: str,
        server_url: str,
        enabled: bool,
        auth_type: str,
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
        target_constraints: dict[str, list[str]] | None = None,
        provenance_type: str = "manual",
    ) -> McpServer:
        async with self.async_session() as session:
            server = McpServer(
                workspace_id=workspace_id,
                scope_type=self._scope_type(target_type),
                agent_id=target_id if target_type == "agent" else None,
                target_id=target_id,
                target_type=target_type,
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
                target_constraints=target_constraints or {},
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
        target_id: str,
        server_id: str,
        patch: dict,
        target_type: str,
    ) -> McpServer | None:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return None
        async with self.async_session() as session:
            result = await session.execute(
                select(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == self._scope_type(target_type),
                    McpServer.target_id == target_id,
                    McpServer.target_type == target_type,
                )
            )
            server = result.scalars().first()
            if not server:
                return None
            expected_revision = patch.pop("expected_revision", None)
            if expected_revision is not None and server.revision != expected_revision:
                raise ValueError("MCP server revision does not match")
            for key, value in patch.items():
                setattr(server, key, value)
            server.revision = int(server.revision or 1) + 1
            await session.commit()
            await session.refresh(server)
            return server

    async def delete_server(
        self, workspace_id: str, target_id: str, server_id: str, target_type: str
    ) -> bool:
        normalized_server_id = self._normalize_server_id(server_id)
        if normalized_server_id is None:
            return False
        async with self.async_session() as session:
            result = await session.execute(
                delete(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == self._scope_type(target_type),
                    McpServer.target_id == target_id,
                    McpServer.target_type == target_type,
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def close(self):
        await self.engine.dispose()
