from app.config.settings import settings
from app.secrets.db_secret_store import DbSecretStore
from app.secrets.interface import SecretStore
from app.secrets.vault_secret_store import VaultSecretStore


def _create_secret_store() -> SecretStore:
    backend = settings.SECRETS_BACKEND.strip().lower()
    if backend == "database":
        return DbSecretStore(settings.DATABASE_URL)

    if backend == "vault":
        if not settings.VAULT_ADDR or not settings.VAULT_TOKEN:
            raise ValueError("VAULT_ADDR and VAULT_TOKEN must be set when SECRETS_BACKEND=vault")
        return VaultSecretStore(
            vault_addr=settings.VAULT_ADDR,
            vault_token=settings.VAULT_TOKEN,
            mount=settings.VAULT_MOUNT,
            path_prefix=settings.VAULT_PATH_PREFIX,
            timeout_ms=settings.VAULT_TIMEOUT_MS,
            verify_tls=settings.VAULT_VERIFY_TLS,
            namespace=settings.VAULT_NAMESPACE,
        )

    raise ValueError(f"Unsupported SECRETS_BACKEND value: {settings.SECRETS_BACKEND}")


secret_store = _create_secret_store()
