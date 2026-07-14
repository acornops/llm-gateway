from types import SimpleNamespace

import pytest

from scripts import seed_db


class FakeSecretStore:
    def __init__(self) -> None:
        self.put_calls = []

    async def put_secret(self, *args, **kwargs) -> None:
        self.put_calls.append((args, kwargs))


class FakeMcpServerRegistry:
    def __init__(self, existing_server=None) -> None:
        self.existing_server = existing_server
        self.create_calls = []

    async def get_server_by_url(self, *args, **kwargs):
        return self.existing_server

    async def create_server(self, *args, **kwargs) -> None:
        self.create_calls.append((args, kwargs))


class FakeToolRegistry:
    def __init__(self, existing_tool=None) -> None:
        self.existing_tool = existing_tool
        self.upsert_calls = []

    async def get_tool(self, *args, **kwargs):
        return self.existing_tool

    async def upsert_tool(self, *args, **kwargs) -> None:
        self.upsert_calls.append((args, kwargs))


@pytest.mark.anyio
async def test_seed_repairs_missing_mock_mcp_server_without_overwriting_existing_tool(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_servers = FakeMcpServerRegistry(existing_server=None)
    fake_tools = FakeToolRegistry(existing_tool=SimpleNamespace(enabled=False))

    monkeypatch.setattr(seed_db, "mcp_server_registry", fake_servers)
    monkeypatch.setattr(seed_db, "tool_registry", fake_tools)
    monkeypatch.setattr(seed_db, "secret_store", FakeSecretStore())

    await seed_db.seed()

    assert len(fake_servers.create_calls) == 1
    server_args, _ = fake_servers.create_calls[0]
    assert server_args == (
        seed_db.EXAMPLE_WORKSPACE_ID,
        seed_db.EXAMPLE_TARGET_ID,
        seed_db.KUBERNETES_TARGET_TYPE,
        "remote-mcp-server",
        "http://mock-mcp:8002/mcp",
        True,
        "none",
    )
    assert fake_tools.upsert_calls == []


@pytest.mark.anyio
async def test_seed_creates_missing_mock_tool_without_recreating_existing_server(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_servers = FakeMcpServerRegistry(existing_server=SimpleNamespace(enabled=True))
    fake_tools = FakeToolRegistry(existing_tool=None)

    monkeypatch.setattr(seed_db, "mcp_server_registry", fake_servers)
    monkeypatch.setattr(seed_db, "tool_registry", fake_tools)
    monkeypatch.setattr(seed_db, "secret_store", FakeSecretStore())

    await seed_db.seed()

    assert fake_servers.create_calls == []
    assert len(fake_tools.upsert_calls) == 1
    _, tool_kwargs = fake_tools.upsert_calls[0]
    assert tool_kwargs["tool_name"] == "get_weather"
    assert tool_kwargs["mcp_server_url"] == "http://mock-mcp:8002/mcp"
    assert tool_kwargs["enabled"] is True
