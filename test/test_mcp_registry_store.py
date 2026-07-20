import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.mcp.registry.store import McpServerRegistry, ToolRegistry
from app.secrets.db_models import Base


async def _create_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _create_server(
    registry: McpServerRegistry,
    *,
    server_name: str,
    server_url: str,
    target_type: str = "kubernetes",
):
    return await registry.create_server(
        workspace_id="ws-1",
        target_id="cluster-a",
        target_type=target_type,
        server_name=server_name,
        server_url=server_url,
        enabled=True,
        auth_type="none",
    )


@pytest.mark.anyio
async def test_tool_registry_crud_and_source_cleanup(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}"
    await _create_schema(database_url)
    registry = ToolRegistry(database_url)
    server_registry = McpServerRegistry(database_url)
    try:
        server = await _create_server(
            server_registry, server_name="server-a", server_url="http://server-a"
        )
        created = await registry.upsert_tool(
            tool_name="github.search",
            mcp_server_url="http://server-a",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(server.id),
            enabled=False,
            description="search repos",
            source="mcp",
        )

        assert (
            await registry.get_tool(
                "ws-1",
                "cluster-a",
                "github.search",
                target_type="kubernetes",
            )
        ) is None
        assert (
            await registry.get_tool(
                "ws-1",
                "cluster-a",
                "github.search",
                target_type="virtual_machine",
                include_disabled=True,
            )
        ) is None
        included = await registry.get_tool(
            "ws-1",
            "cluster-a",
            "github.search",
            target_type="kubernetes",
            server_id=str(server.id),
            include_disabled=True,
        )
        assert included is not None
        assert created.id == included.id

        updated = await registry.upsert_tool(
            tool_name="github.search",
            mcp_server_url="http://server-a",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(server.id),
            enabled=True,
            description="updated description",
            source="builtin",
        )
        listed_names = await registry.list_target_tool_names(
            "ws-1", "cluster-a", target_type="kubernetes"
        )
        assert listed_names == ["github.search"]
        assert await registry.list_target_tool_names(
            "ws-1", "cluster-a", target_type="virtual_machine"
        ) == []
        assert updated.description == "updated description"
        assert updated.enabled is True

        assert await registry.remove_tool_for_target(
            "github.search", "ws-1", "cluster-a", target_type="kubernetes"
        ) is True
        assert await registry.remove_tool_for_target(
            "github.search", "ws-1", "cluster-a", target_type="kubernetes"
        ) is False

        await registry.upsert_tool(
            tool_name="tool.one",
            mcp_server_url="http://server-a",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(server.id),
            source="mcp",
        )
        await registry.upsert_tool(
            tool_name="tool.two",
            mcp_server_url="http://server-a",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(server.id),
            source="builtin",
        )
        await registry.delete_target_tools_by_source(
            "ws-1", "cluster-a", "kubernetes", "mcp"
        )

        remaining = await registry.list_target_tool_names(
            "ws-1", "cluster-a", target_type="kubernetes"
        )
        assert remaining == ["tool.two"]
    finally:
        await registry.close()
        await server_registry.close()


@pytest.mark.anyio
async def test_tool_registry_allows_same_tool_name_from_distinct_servers(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rebind.db'}"
    await _create_schema(database_url)
    registry = ToolRegistry(database_url)
    server_registry = McpServerRegistry(database_url)
    try:
        first_server = await _create_server(
            server_registry, server_name="server-a", server_url="http://server-a"
        )
        second_server = await _create_server(
            server_registry, server_name="server-b", server_url="http://server-b"
        )
        first = await registry.upsert_tool(
            tool_name="github.search",
            mcp_server_url="http://server-a",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(first_server.id),
        )

        second = await registry.upsert_tool(
            tool_name="github.search",
            mcp_server_url="http://server-b",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(second_server.id),
        )

        tools = await registry.list_target_tools(
            "ws-1", "cluster-a", target_type="kubernetes"
        )
        assert len(tools) == 2
        assert {tool.server_id for tool in tools} == {first.server_id, second.server_id}
        assert await registry.get_tool(
            "ws-1", "cluster-a", "github.search", target_type="kubernetes"
        ) is None
    finally:
        await registry.close()
        await server_registry.close()


@pytest.mark.anyio
async def test_tool_registry_rejects_target_type_mismatch_for_existing_tool(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'target-type-mismatch.db'}"
    await _create_schema(database_url)
    registry = ToolRegistry(database_url)
    server_registry = McpServerRegistry(database_url)
    try:
        server = await _create_server(
            server_registry, server_name="server-a", server_url="http://server-a"
        )
        await registry.upsert_tool(
            tool_name="github.search",
            mcp_server_url="http://server-a",
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_id=str(server.id),
        )

        with pytest.raises(ValueError, match="destination does not match"):
            await registry.upsert_tool(
                tool_name="github.search",
                mcp_server_url="http://server-a",
                workspace_id="ws-1",
                target_id="cluster-a",
                target_type="virtual_machine",
                server_id=str(server.id),
            )

        assert (
            await registry.get_tool(
                "ws-1",
                "cluster-a",
                "github.search",
                target_type="kubernetes",
            )
        ) is not None
        assert (
            await registry.get_tool(
                "ws-1",
                "cluster-a",
                "github.search",
                target_type="virtual_machine",
            )
        ) is None
    finally:
        await registry.close()
        await server_registry.close()


@pytest.mark.anyio
async def test_mcp_server_registry_crud_and_invalid_id_handling(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'servers.db'}"
    await _create_schema(database_url)
    registry = McpServerRegistry(database_url)
    try:
        created = await registry.create_server(
            workspace_id="ws-1",
            target_id="cluster-a",
            target_type="kubernetes",
            server_name="github",
            server_url="https://github.example/mcp",
            enabled=True,
            auth_type="none",
        )

        listed = await registry.list_servers("ws-1", "cluster-a", "kubernetes")
        assert [server.server_name for server in listed] == ["github"]
        assert await registry.list_servers(
            "ws-1", "cluster-a", target_type="virtual_machine"
        ) == []
        assert await registry.get_server(
            "ws-1", "cluster-a", str(created.id), target_type="kubernetes"
        ) is not None
        assert await registry.get_server(
            "ws-1", "cluster-a", str(created.id), target_type="virtual_machine"
        ) is None
        assert await registry.get_server(
            "ws-1", "cluster-a", "not-a-uuid", target_type="kubernetes"
        ) is None
        assert (
            await registry.get_server_by_url(
                "ws-1",
                "cluster-a",
                "https://github.example/mcp",
                target_type="kubernetes",
            )
        ) is not None
        assert (
            await registry.get_server_by_url(
                "ws-1",
                "cluster-a",
                "https://github.example/mcp",
                target_type="virtual_machine",
            )
        ) is None

        updated = await registry.update_server(
            "ws-1",
            "cluster-a",
            str(created.id),
            {"enabled": False, "connection_status": "error"},
            target_type="kubernetes",
        )
        assert updated is not None
        assert updated.enabled is False
        assert updated.connection_status == "error"
        assert (
            await registry.update_server(
                "ws-1",
                "cluster-a",
                "not-a-uuid",
                {"enabled": True},
                target_type="kubernetes",
            )
            is None
        )

        assert await registry.delete_server(
            "ws-1", "cluster-a", str(created.id), target_type="kubernetes"
        ) is True
        assert await registry.delete_server(
            "ws-1", "cluster-a", str(uuid.uuid4()), target_type="kubernetes"
        ) is False
        assert await registry.delete_server(
            "ws-1", "cluster-a", "not-a-uuid", target_type="kubernetes"
        ) is False
    finally:
        await registry.close()


@pytest.mark.anyio
async def test_mcp_server_registry_isolates_agent_and_target_destinations(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'server-scope-isolation.db'}"
    await _create_schema(database_url)
    registry = McpServerRegistry(database_url)
    try:
        target = await registry.create_server(
            workspace_id="ws-1",
            target_id="destination-a",
            target_type="kubernetes",
            server_name="operations",
            server_url="https://operations.example/mcp",
            enabled=True,
            auth_type="none",
        )
        agent = await registry.create_server(
            workspace_id="ws-1",
            target_id="destination-a",
            target_type="agent",
            server_name="operations",
            server_url="https://operations.example/mcp",
            enabled=True,
            auth_type="none",
        )

        assert target.scope_type == "target"
        assert target.agent_id is None
        assert agent.scope_type == "agent"
        assert agent.agent_id == "destination-a"
        assert [server.id for server in await registry.list_servers(
            "ws-1", "destination-a", "kubernetes"
        )] == [target.id]
        assert [server.id for server in await registry.list_servers(
            "ws-1", "destination-a", "agent"
        )] == [agent.id]

        with pytest.raises(ValueError, match="revision"):
            await registry.update_server(
                "ws-1",
                "destination-a",
                str(target.id),
                {"enabled": False, "expected_revision": 99},
                target_type="kubernetes",
            )
    finally:
        await registry.close()
