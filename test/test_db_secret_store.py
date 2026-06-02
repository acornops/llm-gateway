import json
import time

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.secrets.db_models import Base, Secret
from app.secrets.db_secret_store import DbSecretStore


async def _create_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_db_secret_store_prefers_exact_then_wildcard_then_workspace_scope(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}"
    await _create_schema(database_url)
    store = DbSecretStore(database_url)
    try:
        await store.put_secret("provider_api_key", "workspace-secret", {"workspace_id": "ws-1"})
        await store.put_secret(
            "provider_api_key",
            "wildcard-secret",
            {"workspace_id": "ws-1", "target_id": "*", "target_type": "kubernetes"},
        )
        await store.put_secret(
            "provider_api_key",
            "exact-secret",
            {
                "workspace_id": "ws-1",
                "target_id": "cluster-a",
                "target_type": "kubernetes",
            },
        )

        assert await store.get_secret(
            "provider_api_key",
            {
                "workspace_id": "ws-1",
                "target_id": "cluster-a",
                "target_type": "kubernetes",
            },
        ) == "exact-secret"
        assert await store.get_secret(
            "provider_api_key",
            {
                "workspace_id": "ws-1",
                "target_id": "cluster-b",
                "target_type": "kubernetes",
            },
        ) == "wildcard-secret"
        assert await store.get_secret(
            "provider_api_key",
            {"workspace_id": "ws-1"},
        ) == "workspace-secret"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_secret_store_serves_cached_secret_after_database_row_removed(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"
    await _create_schema(database_url)
    store = DbSecretStore(database_url)
    scope = {
        "workspace_id": "ws-1",
        "target_id": "cluster-a",
        "target_type": "kubernetes",
    }
    try:
        await store.put_secret("provider_api_key", "cached-secret", scope)

        assert await store.get_secret("provider_api_key", scope) == "cached-secret"

        async with store.async_session() as session:
            await session.execute(delete(Secret))
            await session.commit()

        assert await store.get_secret("provider_api_key", scope) == "cached-secret"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_secret_store_does_not_cache_plaintext_when_ttl_disabled(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'no-cache.db'}"
    await _create_schema(database_url)
    store = DbSecretStore(database_url)
    store._cache_ttl = 0
    scope = {
        "workspace_id": "ws-1",
        "target_id": "cluster-a",
        "target_type": "kubernetes",
    }
    try:
        await store.put_secret("provider_api_key", "uncached-secret", scope)

        assert await store.get_secret("provider_api_key", scope) == "uncached-secret"
        assert store._cache == {}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_secret_store_rejects_incomplete_target_scope(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}"
    await _create_schema(database_url)
    store = DbSecretStore(database_url)
    try:
        with pytest.raises(ValueError, match="target_id and target_type"):
            await store.get_secret(
                "provider_api_key",
                {"workspace_id": "ws-1", "target_id": "cluster-a"},
            )

        with pytest.raises(ValueError, match="target_id and target_type"):
            await store.put_secret(
                "provider_api_key",
                "secret",
                {"workspace_id": "ws-1", "target_type": "kubernetes"},
            )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_secret_store_updates_existing_secret_version_and_health_checks(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'update.db'}"
    await _create_schema(database_url)
    store = DbSecretStore(database_url)
    scope = {
        "workspace_id": "ws-1",
        "target_id": "cluster-a",
        "target_type": "kubernetes",
    }
    try:
        await store.put_secret("provider_api_key", "first-value", scope)
        await store.put_secret("provider_api_key", "second-value", scope)

        assert await store.get_secret("provider_api_key", scope) == "second-value"

        async with store.async_session() as session:
            result = await session.execute(select(Secret.version))
            versions = list(result.scalars())

        assert versions == [2]
        await store.health_check()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_secret_store_publish_and_listener_handle_invalidation(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'listener.db'}"
    await _create_schema(database_url)
    store = DbSecretStore(database_url)

    published: list[tuple[str, str]] = []

    class FakeRedis:
        def __init__(self, pubsub):
            self._pubsub = pubsub
            self.closed = False

        async def publish(self, channel: str, payload: str):
            published.append((channel, payload))

        def pubsub(self):
            return self._pubsub

        async def aclose(self):
            self.closed = True

    class FakePubSub:
        def __init__(self):
            self.subscribed: list[str] = []
            self.closed = False

        async def subscribe(self, channel: str):
            self.subscribed.append(channel)

        async def close(self):
            self.closed = True

        async def listen(self):
            yield {"type": "subscribe", "data": 1}
            yield {
                "type": "message",
                "data": json.dumps({"secret_name": "provider_api_key"}).encode(),
            }

    pubsub = FakePubSub()
    store._redis = FakeRedis(pubsub)
    store._cache[("provider_api_key", '{"target_id": "a"}')] = ("one", time.time() + 60)
    try:
        await store._publish_secret_invalidation("provider_api_key")
        await store.start_cache_invalidation_listener()
        await store._listener_task

        assert published == [
            (
                "gateway:secret-cache-invalidation",
                json.dumps({"secret_name": "provider_api_key"}),
            )
        ]
        assert pubsub.subscribed == ["gateway:secret-cache-invalidation"]
        assert ("provider_api_key", '{"target_id": "a"}') not in store._cache
    finally:
        await store.close()
