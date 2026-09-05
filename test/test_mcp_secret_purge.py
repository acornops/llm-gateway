import json

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.secrets.db_models import Base, Secret
from app.secrets.db_secret_store import (
    SECRET_CACHE_INVALIDATION_CHANNEL,
    DbSecretStore,
)
from app.secrets.vault_secret_store import VaultSecretStore

SERVER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SERVER_ALIAS = SERVER_ID.replace("-", "").upper()
CATALOG_SOURCE_SECRET = "catalog_source::bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CATALOG_BOOTSTRAP_SECRET = "catalog_bootstrap::0123456789abcdef01234567"


@pytest.mark.anyio
async def test_db_mcp_purge_matches_uuid_alias_and_preserves_other_secrets(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'mcp-purge.db'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    store = DbSecretStore(database_url)
    scope = {"workspace_id": "ws-1"}
    deleted_names = [
        f"mcp_credential::ws-1::{SERVER_ID}::user::user-1",
        f"mcp_credential::ws-1::{SERVER_ALIAS}::user::user-1",
        f"mcp_oauth_tokens::ws-1::{{{SERVER_ID.upper()}}}::user::user-1",
    ]
    retained_names = [
        f"mcp_credential::ws-1::{SERVER_ID}::installation",
        "mcp_credential::ws-1::bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb::user::user-1",
        "provider_api_key",
    ]

    class FakeRedis:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []

        async def publish(self, channel: str, payload: str) -> None:
            self.published.append((channel, payload))

        async def aclose(self) -> None:
            pass

    redis = FakeRedis()
    store._redis = redis
    try:
        for name in deleted_names + retained_names:
            await store.put_secret(name, "secret", scope)
            await store.get_secret(name, scope)
        redis.published.clear()

        assert (
            await store.count_mcp_secrets(
                "ws-1",
                server_id=SERVER_ID,
                owner_type="user",
                owner_id="user-1",
            )
            == 3
        )
        assert (
            await store.purge_mcp_secrets(
                "ws-1",
                server_id=SERVER_ID,
                owner_type="user",
                owner_id="user-1",
            )
            == 3
        )

        async with store.async_session() as session:
            remaining = set((await session.execute(select(Secret.secret_name))).scalars())
        assert remaining == set(retained_names)
        assert all(cache_key[0] in retained_names for cache_key in store._cache)
        assert {
            json.loads(payload)["secret_name"]
            for channel, payload in redis.published
            if channel == SECRET_CACHE_INVALIDATION_CHANNEL
        } == set(deleted_names)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_vault_mcp_purge_lists_metadata_and_deletes_uuid_aliases() -> None:
    names = [
        f"mcp_credential::ws-1::{SERVER_ALIAS}::user::user-1",
        f"mcp_oauth_tokens::ws-1::{SERVER_ID}::user::user-1",
        f"mcp_credential::ws-1::{SERVER_ID}::installation",
        "not_mcp",
    ]
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "LIST":
            return httpx.Response(200, json={"data": {"keys": names}})
        if request.method == "DELETE":
            deleted_paths.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    store._client = httpx.AsyncClient(
        base_url="http://vault.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (
            await store.purge_mcp_secrets(
                "ws-1",
                server_id=SERVER_ID,
                owner_type="user",
                owner_id="user-1",
            )
            == 2
        )
    finally:
        await store.close()

    assert len(deleted_paths) == 2
    assert all("/metadata/acornops/ws-1/_global/" in path for path in deleted_paths)


@pytest.mark.anyio
async def test_vault_health_requires_metadata_list_capability() -> None:
    list_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sys/health":
            return httpx.Response(200)
        if request.method == "LIST":
            list_paths.append(request.url.path)
            return httpx.Response(403)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    store._client = httpx.AsyncClient(
        base_url="http://vault.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="list capability"):
            await store.health_check()
    finally:
        await store.close()

    assert list_paths == ["/v1/secret/metadata/acornops/_readiness/_global"]


@pytest.mark.anyio
async def test_vault_health_accepts_missing_shaped_inventory_sentinel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sys/health":
            return httpx.Response(200)
        if request.method == "LIST":
            assert request.url.path == ("/v1/secret/metadata/acornops/_readiness/_global")
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    store._client = httpx.AsyncClient(
        base_url="http://vault.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        await store.health_check()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_generated_catalog_purge_is_strict_and_workspace_scoped(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'catalog-purge.db'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    store = DbSecretStore(database_url)
    deleted = [CATALOG_SOURCE_SECRET, CATALOG_BOOTSTRAP_SECRET]
    retained = [
        "catalog_source::old",
        f"mcp_credential::ws-1::{SERVER_ID}::installation",
        "provider_api_key",
    ]
    try:
        for name in deleted + retained:
            await store.put_secret(name, "secret", {"workspace_id": "ws-1"})
        await store.put_secret(
            CATALOG_SOURCE_SECRET,
            "other-workspace",
            {"workspace_id": "ws-2"},
        )

        assert await store.count_generated_catalog_secrets("ws-1") == 2
        assert await store.purge_generated_catalog_secrets("ws-1") == 2
        assert await store.count_generated_catalog_secrets("ws-1") == 0

        async with store.async_session() as session:
            rows = list(
                (await session.execute(select(Secret.tenant_scope, Secret.secret_name))).all()
            )
        assert ({"workspace_id": "ws-2"}, CATALOG_SOURCE_SECRET) in rows
        assert {name for scope, name in rows if scope == {"workspace_id": "ws-1"}} == set(retained)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_vault_generated_catalog_purge_preserves_non_generated_names() -> None:
    names = [
        CATALOG_SOURCE_SECRET,
        CATALOG_BOOTSTRAP_SECRET,
        "catalog_source::old",
        "provider_api_key",
    ]
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "LIST":
            return httpx.Response(200, json={"data": {"keys": names}})
        if request.method == "DELETE":
            deleted_paths.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    store._client = httpx.AsyncClient(
        base_url="http://vault.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await store.purge_generated_catalog_secrets("ws-1") == 2
    finally:
        await store.close()

    assert len(deleted_paths) == 2
    assert all("/metadata/acornops/ws-1/_global/" in path for path in deleted_paths)


@pytest.mark.anyio
async def test_db_global_user_reset_preserves_installation_and_provider_secrets(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'global-user-reset.db'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    store = DbSecretStore(database_url)
    user_names = [
        f"mcp_credential::ws-1::{SERVER_ID}::user::former-user",
        f"mcp_oauth_tokens::ws-2::{SERVER_ALIAS}::user::former-user",
    ]
    try:
        await store.put_secret(user_names[0], "user", {"workspace_id": "ws-1"})
        await store.put_secret(user_names[1], "oauth", {"workspace_id": "ws-2"})
        await store.put_secret(
            f"mcp_credential::ws-1::{SERVER_ID}::installation",
            "installation",
            {"workspace_id": "ws-1"},
        )
        await store.put_secret("provider_api_key", "provider", {"workspace_id": "ws-1"})

        assert await store.count_all_mcp_user_secrets() == 2
        assert await store.count_all_mcp_installation_secrets() == 1
        assert await store.purge_all_mcp_user_secrets() == 2
        assert await store.count_all_mcp_user_secrets() == 0
        assert await store.count_all_mcp_installation_secrets() == 1

        async with store.async_session() as session:
            remaining = set((await session.execute(select(Secret.secret_name))).scalars())
        assert remaining == {
            f"mcp_credential::ws-1::{SERVER_ID}::installation",
            "provider_api_key",
        }
    finally:
        await store.close()


@pytest.mark.anyio
async def test_db_global_user_reset_chunks_atomically_and_retries(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'global-user-reset-chunks.db'}"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    store = DbSecretStore(database_url)
    scope = {"workspace_id": "ws-1"}
    async with store.async_session() as session:
        session.add_all(
            [
                Secret(
                    tenant_scope=scope,
                    secret_name=(
                        f"mcp_credential::ws-1::{SERVER_ID}::user::former-{index}"
                    ),
                    ciphertext=b"ciphertext",
                    nonce=b"012345678901",
                    aad=b"aad",
                    key_id="test-key",
                    version=1,
                )
                for index in range(501)
            ]
        )
        await session.commit()

    delete_count = 0

    def fail_second_delete(_conn, _cursor, statement, _params, _context, _many):
        nonlocal delete_count
        if statement.lstrip().upper().startswith("DELETE"):
            delete_count += 1
            if delete_count == 2:
                raise RuntimeError("injected second chunk failure")

    event.listen(store.engine.sync_engine, "before_cursor_execute", fail_second_delete)
    try:
        with pytest.raises(RuntimeError, match="second chunk failure"):
            await store.purge_all_mcp_user_secrets()
    finally:
        event.remove(store.engine.sync_engine, "before_cursor_execute", fail_second_delete)

    try:
        assert await store.count_all_mcp_user_secrets() == 501
        assert await store.purge_all_mcp_user_secrets() == 501
        assert await store.count_all_mcp_user_secrets() == 0
    finally:
        await store.close()


@pytest.mark.anyio
async def test_vault_global_user_reset_enumerates_former_member_workspaces() -> None:
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "LIST" and path == "/v1/secret/metadata/acornops":
            return httpx.Response(200, json={"data": {"keys": ["ws-1/", "ws-2/"]}})
        if request.method == "LIST" and path.endswith("/ws-1/_global"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "keys": [
                            f"mcp_credential::ws-1::{SERVER_ID}::user::former",
                            f"mcp_credential::ws-1::{SERVER_ID}::installation",
                        ]
                    }
                },
            )
        if request.method == "LIST" and path.endswith("/ws-2/_global"):
            return httpx.Response(
                200,
                json={"data": {"keys": [f"mcp_oauth_tokens::ws-2::{SERVER_ALIAS}::user::former"]}},
            )
        if request.method == "DELETE":
            deleted_paths.append(path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    store._client = httpx.AsyncClient(
        base_url="http://vault.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await store.purge_all_mcp_user_secrets() == 2
    finally:
        await store.close()

    assert len(deleted_paths) == 2
    assert all("::installation" not in path for path in deleted_paths)
