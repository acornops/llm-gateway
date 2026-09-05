import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.catalog.adapter import CatalogPage, NormalizedMcpArtifact
from app.catalog.models import CatalogArtifact
from app.catalog.store import CatalogStore
from app.secrets.db_models import Base


@pytest.mark.anyio
async def test_stale_catalog_sync_cannot_commit_after_aba_source_trust_change(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'catalog-sync.db'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    store = CatalogStore(database_url)
    source, binding = await store.create_source(
        workspace_id="ws-1",
        display_name="Registry",
        base_url="https://old-registry.example",
        auth_type="none",
        auth_secret_name=None,
        auth_header_name=None,
        network_route="direct",
        enabled=True,
        management_mode="workspace",
        artifact_kind="mcp_server",
        adapter_type="mcp_registry_v0_1",
        adapter_base_path="/v0.1",
    )
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    class DelayedAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def list_updated(self, **_kwargs):
            fetch_started.set()
            await release_fetch.wait()
            return CatalogPage(items=[{"server": {}}], next_cursor=None)

    artifact = NormalizedMcpArtifact(
        name="io.example/tool",
        title="Tool",
        description="Tool description",
        version="1.0.0",
        digest="sha256:test",
        metadata={},
        payload={},
        compatible=True,
        incompatibility_reason=None,
        remote_endpoints=[],
        published_at=None,
        updated_at=None,
    )
    with (
        patch("app.catalog.store.McpRegistryV01Adapter", DelayedAdapter),
        patch(
            "app.catalog.adapter.normalize_mcp_registry_entry",
            return_value=artifact,
        ),
    ):
        stale_sync = asyncio.create_task(store.sync_mcp_binding(source, binding, incremental=False))
        await fetch_started.wait()
        changed = await store.update_source(
            "ws-1",
            source.id,
            {"base_url": "https://new-registry.example"},
            clear_artifacts=True,
        )
        assert changed is not None
        restored = await store.update_source(
            "ws-1",
            source.id,
            {"base_url": "https://old-registry.example"},
            clear_artifacts=True,
        )
        assert restored is not None
        assert restored[0].base_url == source.base_url
        assert restored[0].authority_generation > source.authority_generation
        release_fetch.set()
        with pytest.raises(ValueError, match="changed while synchronization"):
            await stale_sync

    try:
        async with store.async_session() as session:
            artifact_count = int(
                (
                    await session.execute(select(func.count()).select_from(CatalogArtifact))
                ).scalar_one()
            )
        assert artifact_count == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_later_catalog_sync_supersedes_an_older_in_flight_sync(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'catalog-sync-order.db'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    store = CatalogStore(database_url)
    source, binding = await store.create_source(
        workspace_id="ws-1",
        display_name="Registry",
        base_url="https://registry.example",
        auth_type="none",
        auth_secret_name=None,
        auth_header_name=None,
        network_route="direct",
        enabled=True,
        management_mode="workspace",
        artifact_kind="mcp_server",
        adapter_type="mcp_registry_v0_1",
        adapter_base_path="/v0.1",
    )
    first_fetch_started = asyncio.Event()
    release_first_fetch = asyncio.Event()
    adapter_count = 0

    class OrderedAdapter:
        def __init__(self, *_args, **_kwargs):
            nonlocal adapter_count
            adapter_count += 1
            self.order = adapter_count

        async def list_updated(self, **_kwargs):
            if self.order == 1:
                first_fetch_started.set()
                await release_first_fetch.wait()
                return CatalogPage(items=[{"order": "old"}], next_cursor=None)
            return CatalogPage(items=[{"order": "new"}], next_cursor=None)

    def normalize(item):
        return NormalizedMcpArtifact(
            name="io.example/tool",
            title="Tool",
            description="Tool description",
            version="1.0.0",
            digest=f"sha256:{item['order']}",
            metadata={},
            payload={},
            compatible=True,
            incompatibility_reason=None,
            remote_endpoints=[],
            published_at=None,
            updated_at=None,
        )

    with (
        patch("app.catalog.store.McpRegistryV01Adapter", OrderedAdapter),
        patch(
            "app.catalog.adapter.normalize_mcp_registry_entry",
            side_effect=normalize,
        ),
    ):
        older = asyncio.create_task(
            store.sync_mcp_binding(source, binding, incremental=False)
        )
        await first_fetch_started.wait()
        assert await store.sync_mcp_binding(source, binding, incremental=False) == 1
        release_first_fetch.set()
        with pytest.raises(ValueError, match="changed while synchronization"):
            await older

    try:
        async with store.async_session() as session:
            digest = (
                await session.execute(select(CatalogArtifact.digest))
            ).scalar_one()
        assert digest == "sha256:new"
    finally:
        await store.close()
