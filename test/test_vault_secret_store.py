import httpx
import pytest

from app.resilience.outbound import dependency_circuit_breaker
from app.secrets.errors import SecretNotFoundError
from app.secrets.vault_secret_store import VaultSecretStore


@pytest.mark.anyio
async def test_vault_secret_store_candidate_scopes_do_not_duplicate_workspace_scope() -> None:
    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    try:
        assert store._candidate_scopes({"workspace_id": "workspace"}) == [
            {"workspace_id": "workspace"}
        ]
        assert store._candidate_scopes(
            {
                "workspace_id": "workspace",
                "target_id": "cluster",
                "target_type": "kubernetes",
            }
        ) == [
            {
                "workspace_id": "workspace",
                "target_id": "cluster",
                "target_type": "kubernetes",
            },
            {
                "workspace_id": "workspace",
                "target_id": "*",
                "target_type": "kubernetes",
            },
            {"workspace_id": "workspace"},
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_vault_secret_store_does_not_cache_plaintext_when_ttl_disabled() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            status_code=200,
            json={"data": {"data": {"value": f"secret-value-{requests}"}}},
        )

    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )
    store._cache_ttl = 0
    store._client = httpx.AsyncClient(
        base_url="http://vault.example",
        transport=httpx.MockTransport(handler),
    )

    try:
        assert await store.get_secret(
            "openai_api_key",
            {
                "workspace_id": "workspace",
                "target_id": "cluster",
                "target_type": "kubernetes",
            },
        ) == "secret-value-1"
        assert await store.get_secret(
            "openai_api_key",
            {
                "workspace_id": "workspace",
                "target_id": "cluster",
                "target_type": "kubernetes",
            },
        ) == "secret-value-2"
        assert store._cache == {}
    finally:
        await store.close()
        await dependency_circuit_breaker.reset()


@pytest.mark.anyio
async def test_vault_secret_store_retries_transient_read_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    await dependency_circuit_breaker.reset()
    monkeypatch.setattr("app.secrets.vault_secret_store.settings.VAULT_READ_BACKOFF_MS", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) < 3:
            raise httpx.ConnectTimeout("vault timeout", request=request)
        return httpx.Response(
            status_code=200,
            json={"data": {"data": {"value": "secret-value"}}},
        )

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
        value = await store.get_secret(
            "openai_api_key",
            {
                "workspace_id": "workspace",
                "target_id": "cluster",
                "target_type": "kubernetes",
            },
        )
    finally:
        await store.close()
        await dependency_circuit_breaker.reset()

    assert value == "secret-value"
    assert requests == [
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/cluster/openai_api_key",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/cluster/openai_api_key",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/cluster/openai_api_key",
    ]


@pytest.mark.anyio
async def test_vault_secret_store_404_resets_circuit_failure_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    await dependency_circuit_breaker.reset()
    monkeypatch.setattr(
        "app.secrets.vault_secret_store.settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        2,
    )
    monkeypatch.setattr("app.secrets.vault_secret_store.settings.VAULT_READ_BACKOFF_MS", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) in (1, 3):
            raise httpx.ConnectTimeout("vault timeout", request=request)
        return httpx.Response(status_code=404, json={"errors": ["missing"]})

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
        with pytest.raises(SecretNotFoundError):
            await store.get_secret(
                "missing_secret",
                {
                    "workspace_id": "workspace",
                    "target_id": "cluster",
                    "target_type": "kubernetes",
                },
            )

        with pytest.raises(SecretNotFoundError):
            await store.get_secret(
                "missing_secret",
                {
                    "workspace_id": "workspace",
                    "target_id": "cluster",
                    "target_type": "kubernetes",
                },
            )
    finally:
        await store.close()
        await dependency_circuit_breaker.reset()

    assert requests == [
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/cluster/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/cluster/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/%2A/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/%2A/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/_global/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/cluster/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/kubernetes/%2A/missing_secret",
        "http://vault.example/v1/secret/data/acornops/workspace/_global/missing_secret",
    ]


@pytest.mark.anyio
async def test_vault_secret_store_rejects_incomplete_target_scope() -> None:
    store = VaultSecretStore(
        vault_addr="http://vault.example",
        vault_token="token",
        mount="secret",
        path_prefix="acornops",
        timeout_ms=1000,
        verify_tls=True,
    )

    try:
        with pytest.raises(ValueError, match="target_id and target_type"):
            await store.get_secret(
                "provider_api_key",
                {"workspace_id": "workspace", "target_id": "cluster"},
            )

        with pytest.raises(ValueError, match="target_id and target_type"):
            await store.put_secret(
                "provider_api_key",
                "secret",
                {"workspace_id": "workspace", "target_type": "kubernetes"},
            )
    finally:
        await store.close()
