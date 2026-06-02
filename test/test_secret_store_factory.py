import pytest

from app.secrets import store


def test_create_secret_store_uses_database_backend(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    sentinel = object()
    monkeypatch.setattr(store.settings, "SECRETS_BACKEND", "database")
    monkeypatch.setattr(store.settings, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    def build_db_store(database_url: str):
        calls.append(database_url)
        return sentinel

    monkeypatch.setattr(store, "DbSecretStore", build_db_store)

    assert store._create_secret_store() is sentinel
    assert calls == ["sqlite+aiosqlite:///:memory:"]


def test_create_secret_store_uses_vault_backend(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    sentinel = object()
    monkeypatch.setattr(store.settings, "SECRETS_BACKEND", "vault")
    monkeypatch.setattr(store.settings, "VAULT_ADDR", "https://vault.example")
    monkeypatch.setattr(store.settings, "VAULT_TOKEN", "vault-token")
    monkeypatch.setattr(store.settings, "VAULT_MOUNT", "secret")
    monkeypatch.setattr(store.settings, "VAULT_PATH_PREFIX", "acornops")
    monkeypatch.setattr(store.settings, "VAULT_TIMEOUT_MS", 1234)
    monkeypatch.setattr(store.settings, "VAULT_VERIFY_TLS", True)
    monkeypatch.setattr(store.settings, "VAULT_NAMESPACE", "team-a")

    def build_vault_store(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(store, "VaultSecretStore", build_vault_store)

    assert store._create_secret_store() is sentinel
    assert calls == [
        {
            "vault_addr": "https://vault.example",
            "vault_token": "vault-token",
            "mount": "secret",
            "path_prefix": "acornops",
            "timeout_ms": 1234,
            "verify_tls": True,
            "namespace": "team-a",
        }
    ]


def test_create_secret_store_requires_vault_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store.settings, "SECRETS_BACKEND", "vault")
    monkeypatch.setattr(store.settings, "VAULT_ADDR", None)
    monkeypatch.setattr(store.settings, "VAULT_TOKEN", None)

    with pytest.raises(ValueError, match="VAULT_ADDR and VAULT_TOKEN must be set"):
        store._create_secret_store()


def test_create_secret_store_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store.settings, "SECRETS_BACKEND", "filesystem")

    with pytest.raises(ValueError, match="Unsupported SECRETS_BACKEND value: filesystem"):
        store._create_secret_store()
