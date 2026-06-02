import time

import pytest

from app.secrets.db_secret_store import DbSecretStore
from app.secrets.vault_secret_store import VaultSecretStore


@pytest.mark.anyio
async def test_db_secret_cache_invalidation_evicts_all_scopes_for_secret():
    store = DbSecretStore("sqlite+aiosqlite:///:memory:")
    try:
        expires_at = time.time() + 60
        store._cache[("provider_api_key", '{"target_id": "a"}')] = ("one", expires_at)
        store._cache[("provider_api_key", '{"target_id": "b"}')] = ("two", expires_at)
        store._cache[("other_secret", '{"target_id": "a"}')] = ("three", expires_at)

        store._evict_secret_cache("provider_api_key")

        assert ("provider_api_key", '{"target_id": "a"}') not in store._cache
        assert ("provider_api_key", '{"target_id": "b"}') not in store._cache
        assert store._cache[("other_secret", '{"target_id": "a"}')] == ("three", expires_at)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_vault_secret_cache_invalidation_evicts_all_scopes_for_secret():
    store = VaultSecretStore(
        vault_addr="http://vault.test",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=False,
    )
    try:
        expires_at = time.time() + 60
        store._cache[("provider_api_key", '{"target_id": "a"}')] = ("one", expires_at)
        store._cache[("provider_api_key", '{"target_id": "b"}')] = ("two", expires_at)
        store._cache[("other_secret", '{"target_id": "a"}')] = ("three", expires_at)

        store._evict_secret_cache("provider_api_key")

        assert ("provider_api_key", '{"target_id": "a"}') not in store._cache
        assert ("provider_api_key", '{"target_id": "b"}') not in store._cache
        assert store._cache[("other_secret", '{"target_id": "a"}')] == ("three", expires_at)
    finally:
        await store.close()
