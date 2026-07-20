import base64
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_VALUES = {
    "change-me",
    "changeme",
    "replace-me",
    "replace_me",
    "replace-me-with-32-byte-base64",
    "dev_orchestrator_token",
    "gateway_password",
    "root",
}

DEFAULT_LOCAL_KEK = "SglIGBscu1EgQ+AlpqJLADNN9QmCzS9d1ZvK3oT/e5s="


def _is_unsafe_secret(value: str | None, minimum_length: int = 32) -> bool:
    if not value:
        return True
    normalized = value.strip().lower()
    return (
        len(normalized) < minimum_length
        or normalized in PLACEHOLDER_VALUES
        or "change-me" in normalized
        or "replace-me" in normalized
    )


def _database_password(database_url: str) -> str | None:
    parsed = urlparse(database_url)
    return unquote(parsed.password) if parsed.password else None


def _is_allowed_internal_http_host(hostname: str, allowed_hosts: set[str]) -> bool:
    service_name = hostname.split(".", 1)[0]
    for allowed_host in allowed_hosts:
        if hostname == allowed_host or service_name == allowed_host:
            return True
        if service_name.endswith(f"-{allowed_host}"):
            return "." not in hostname or ".svc" in hostname
    return False


def _unsafe_url(value: str, *, allow_internal_http_hosts: set[str] | None = None) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if not parsed.scheme or not hostname:
        return True
    if hostname in {"localhost", "127.0.0.1", "::1", "mock-auth"} or hostname.endswith(
        ".localhost"
    ):
        return True
    if parsed.scheme == "https":
        return False
    return parsed.scheme != "http" or not _is_allowed_internal_http_host(
        hostname,
        allow_internal_http_hosts or set(),
    )


class Settings(BaseSettings):
    APP_ENV: str = "development"
    NODE_ENV: str | None = None

    # Server
    GATEWAY_HTTP_ADDR: str = "0.0.0.0"
    GATEWAY_PORT: int = 8001
    LOG_LEVEL: str = "info"
    ENABLE_API_DOCS: bool = False
    MAX_REQUEST_BODY_BYTES: int = 1_000_000
    READINESS_CHECK_TIMEOUT_MS: int = 1000
    INTERNAL_TRANSPORT_TLS_ENABLED: bool = False
    INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT: bool = True
    INTERNAL_TRANSPORT_TLS_CA_FILE: str | None = None
    INTERNAL_TRANSPORT_TLS_CERT_FILE: str | None = None
    INTERNAL_TRANSPORT_TLS_KEY_FILE: str | None = None
    INTERNAL_TRANSPORT_HEALTH_PORT: int | None = None
    ADDITIONAL_CA_BUNDLE_FILE: str = ""

    # Auth
    AUTH_JWKS_URL: str = "http://mock-auth:8003/jwks.json"
    AUTH_ISSUER: str = "llm-gateway"
    AUTH_AUDIENCE: str = "execution-gateway"
    AUTH_CLOCK_SKEW_SEC: int = 30
    JWKS_CACHE_TTL_SECONDS: int = 300
    JWKS_READINESS_MAX_STALENESS_SECONDS: int = 900
    REQUIRE_JWKS_READINESS: bool | None = None
    ADMIN_API_TOKEN: str = "dev_orchestrator_token"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://gateway_user:gateway_password@localhost:5432/gateway"
    DATABASE_POOL_MIN: int = 10
    DATABASE_POOL_MAX: int = 50

    # Redis
    REDIS_URL: str | None = None

    # Secrets
    SECRETS_BACKEND: str = "database"
    SECRETS_KEK_BASE64: str = "SglIGBscu1EgQ+AlpqJLADNN9QmCzS9d1ZvK3oT/e5s="
    SECRETS_CACHE_TTL_SEC: int = 60
    VAULT_ADDR: str | None = None
    VAULT_TOKEN: str | None = None
    VAULT_NAMESPACE: str | None = None
    VAULT_MOUNT: str = "secret"
    VAULT_PATH_PREFIX: str = "acornops"
    VAULT_TIMEOUT_MS: int = 5000
    VAULT_VERIFY_TLS: bool = True

    # LLM
    LLM_DEFAULT_TIMEOUT_MS: int = 60000
    LLM_PROVIDER_OPENAI_ENABLED: bool = True
    LLM_PROVIDER_ANTHROPIC_ENABLED: bool = True
    LLM_PROVIDER_GEMINI_ENABLED: bool = True
    LLM_PROVIDER_OPENAI_BASE_URL: str | None = None
    LLM_PROVIDER_ANTHROPIC_BASE_URL: str | None = None
    LLM_PROVIDER_GEMINI_BASE_URL: str | None = None
    LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES: bool = False
    PROVIDER_RETRY_ATTEMPTS: int = 3
    PROVIDER_RETRY_BACKOFF_MS: int = 200

    # MCP
    BUILTIN_TARGET_MCP_SERVER_URL: str = "http://control-plane:8081/internal/v1/mcp"
    MCP_CALL_DEFAULT_TIMEOUT_MS: int = 10000
    MCP_MAX_TOOL_RESULT_BYTES: int = Field(default=2 * 1024 * 1024, ge=1024, le=2 * 1024 * 1024)
    BUILTIN_MCP_MAX_RESPONSE_BYTES: int = Field(
        default=3 * 1024 * 1024,
        ge=2 * 1024 * 1024 + 64 * 1024,
        le=5 * 1024 * 1024,
    )
    MCP_DISCOVERY_RETRY_ATTEMPTS: int = 3
    MCP_DISCOVERY_RETRY_BACKOFF_MS: int = 200
    MCP_EGRESS_REQUIRE_HTTPS: bool = True
    MCP_EGRESS_ALLOW_PRIVATE_NETWORKS: bool = False
    MCP_EGRESS_ALLOW_LOCAL_ADDRESSES: bool = False
    MCP_EGRESS_ALLOWED_HOSTS: str = ""
    MCP_EGRESS_DNS_CACHE_TTL_SEC: int = 300
    REMOTE_MCP_ENABLED: bool = True
    MCP_CONNECTION_RATE_LIMIT_PER_WINDOW: int = Field(default=10, ge=1, le=1000)
    TOOL_REGISTRY_CACHE_TTL_SEC: int = 300
    # Catalog registry discovery
    CATALOG_OFFICIAL_REGISTRY_ENABLED: bool = False
    CATALOG_OFFICIAL_REGISTRY_URL: str = "https://registry.modelcontextprotocol.io"
    CATALOG_WORKSPACE_MANAGED_SOURCES_ENABLED: bool = True
    CATALOG_BOOTSTRAP_SOURCES_JSON: str = "[]"
    CATALOG_REQUEST_TIMEOUT_MS: int = 10000
    CATALOG_MAX_RESPONSE_BYTES: int = Field(
        default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024
    )
    CATALOG_SYNC_PAGE_SIZE: int = Field(default=100, ge=1, le=500)
    CATALOG_MAX_SYNC_PAGES: int = Field(default=100, ge=1, le=1000)

    # Rate limits
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    LLM_RATE_LIMIT_PER_WINDOW: int = 100
    TOOL_RATE_LIMIT_PER_WINDOW: int = 200
    REQUIRE_REDIS_RATE_LIMITS_IN_PRODUCTION: bool = True

    # Outbound resilience
    OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 3
    OUTBOUND_CIRCUIT_BREAKER_RESET_MS: int = 30000

    # Vault resilience
    VAULT_READ_RETRY_ATTEMPTS: int = 3
    VAULT_READ_BACKOFF_MS: int = 200

    @model_validator(mode="after")
    def validate_production_safety(self):
        runtime_env = (self.NODE_ENV or self.APP_ENV).strip().lower()
        if self.REQUIRE_JWKS_READINESS is None:
            self.REQUIRE_JWKS_READINESS = runtime_env == "production"
        if self.INTERNAL_TRANSPORT_TLS_ENABLED:
            self._validate_internal_transport_tls()
        if self.ADDITIONAL_CA_BUNDLE_FILE:
            try:
                with Path(self.ADDITIONAL_CA_BUNDLE_FILE).open("rb"):
                    pass
            except OSError as error:
                raise ValueError(
                    "ADDITIONAL_CA_BUNDLE_FILE must point to a readable file"
                ) from error
        if runtime_env != "production":
            return self

        errors: list[str] = []
        if _is_unsafe_secret(self.ADMIN_API_TOKEN):
            errors.append("ADMIN_API_TOKEN must be a generated production token")

        if _is_unsafe_secret(_database_password(self.DATABASE_URL), minimum_length=12):
            errors.append("DATABASE_URL must include a non-default production database password")

        if _unsafe_url(
            self.AUTH_JWKS_URL,
            allow_internal_http_hosts={"control-plane"},
        ):
            errors.append(
                "AUTH_JWKS_URL must be an HTTPS URL or the internal control-plane JWKS URL"
            )

        try:
            kek = base64.b64decode(self.SECRETS_KEK_BASE64, validate=True)
            if len(kek) != 32 or self.SECRETS_KEK_BASE64 == DEFAULT_LOCAL_KEK:
                raise ValueError("unsafe kek")
            if _is_unsafe_secret(self.SECRETS_KEK_BASE64):
                raise ValueError("placeholder kek")
        except Exception:
            errors.append("SECRETS_KEK_BASE64 must be a generated base64-encoded 32-byte key")

        secrets_backend = self.SECRETS_BACKEND.strip().lower()
        if secrets_backend not in {"database", "vault"}:
            errors.append("SECRETS_BACKEND must be database or vault")
        if self.SECRETS_CACHE_TTL_SEC != 0:
            errors.append(
                "SECRETS_CACHE_TTL_SEC must be 0 in production to avoid plaintext secret caching"
            )
        if secrets_backend == "vault":
            if _unsafe_url(self.VAULT_ADDR or ""):
                errors.append(
                    "VAULT_ADDR must be a production HTTPS URL when SECRETS_BACKEND=vault"
                )
            if _is_unsafe_secret(self.VAULT_TOKEN):
                errors.append(
                    "VAULT_TOKEN must be a generated production token when SECRETS_BACKEND=vault"
                )
            if not self.VAULT_VERIFY_TLS:
                errors.append("VAULT_VERIFY_TLS must remain enabled in production")

        if self.REQUIRE_REDIS_RATE_LIMITS_IN_PRODUCTION and not self.REDIS_URL:
            errors.append(
                "REDIS_URL is required in production when rate limits are configured to fail closed"
            )
        if self.LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES:
            errors.append("LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES must be false in production")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _validate_internal_transport_tls(self) -> None:
        for field_name in ("INTERNAL_TRANSPORT_TLS_CERT_FILE", "INTERNAL_TRANSPORT_TLS_KEY_FILE"):
            path = getattr(self, field_name)
            if not path or not Path(path).is_file():
                raise ValueError(
                    f"{field_name} must point to a readable file "
                    "when internal transport TLS is enabled"
                )
        ca_file = self.INTERNAL_TRANSPORT_TLS_CA_FILE
        if not ca_file or not Path(ca_file).is_file():
            raise ValueError(
                "INTERNAL_TRANSPORT_TLS_CA_FILE must point to a readable file "
                "when internal transport TLS is enabled"
            )
        for field_name in ("AUTH_JWKS_URL", "BUILTIN_TARGET_MCP_SERVER_URL"):
            value = getattr(self, field_name)
            if urlparse(value).scheme != "https":
                raise ValueError(
                    f"{field_name} must use https when internal transport TLS is enabled"
                )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
