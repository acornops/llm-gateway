import asyncio
import contextlib
import json
import time
from urllib.parse import quote, unquote

import httpx
import structlog
from redis.asyncio import Redis

from app.config.settings import settings
from app.outbound_tls import httpx_verify, redis_tls_kwargs
from app.resilience.outbound import (
    CircuitOpenError,
    backoff_seconds,
    dependency_circuit_breaker,
    is_retryable_dependency_error,
    note_dependency_event,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.interface import SecretStore
from app.secrets.mcp_names import (
    McpSecretOwnerType,
    matches_generated_catalog_secret_name,
    matches_mcp_secret_name,
)

logger = structlog.get_logger()
SECRET_CACHE_INVALIDATION_CHANNEL = "gateway:secret-cache-invalidation"


class VaultSecretStore(SecretStore):
    """
    HashiCorp Vault KV-v2 secret store with in-memory caching.
    """

    def __init__(
        self,
        vault_addr: str,
        vault_token: str,
        mount: str,
        path_prefix: str,
        timeout_ms: int,
        verify_tls: bool,
        namespace: str | None = None,
    ):
        self._vault_token = vault_token
        self._mount = mount.strip("/")
        self._path_prefix = path_prefix.strip("/")
        self._namespace = namespace
        self._client = httpx.AsyncClient(
            base_url=vault_addr.rstrip("/"),
            timeout=timeout_ms / 1000.0,
            verify=httpx_verify(verify_tls=verify_tls),
        )
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._cache_ttl = settings.SECRETS_CACHE_TTL_SEC
        self._redis = (
            Redis.from_url(settings.REDIS_URL, **redis_tls_kwargs(settings.REDIS_URL))
            if settings.REDIS_URL
            else None
        )
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"X-Vault-Token": self._vault_token}
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        return headers

    def _candidate_scopes(self, tenant_scope: dict[str, str]) -> list[dict[str, str]]:
        self._validate_scope(tenant_scope)
        candidate_scopes = [tenant_scope]
        if "target_id" in tenant_scope:
            wildcard_scope = dict(tenant_scope)
            wildcard_scope["target_id"] = "*"
            candidate_scopes.append(wildcard_scope)
        workspace_scope = (
            {"workspace_id": tenant_scope["workspace_id"]}
            if "workspace_id" in tenant_scope
            else None
        )
        if workspace_scope and workspace_scope not in candidate_scopes:
            candidate_scopes.append(workspace_scope)
        return candidate_scopes

    def _validate_scope(self, tenant_scope: dict[str, str]) -> None:
        has_target_id = "target_id" in tenant_scope
        has_target_type = "target_type" in tenant_scope
        if has_target_id != has_target_type:
            raise ValueError("Secret target scope requires both target_id and target_type")

    def _path_for_scope(self, secret_name: str, tenant_scope: dict[str, str]) -> str:
        self._validate_scope(tenant_scope)
        workspace_id = tenant_scope.get("workspace_id", "_global")
        target_id = tenant_scope.get("target_id", "_global")
        parts: list[str] = []
        if self._path_prefix:
            parts.extend(self._path_prefix.split("/"))
        if "target_type" in tenant_scope:
            parts.extend([workspace_id, tenant_scope["target_type"], target_id, secret_name])
        else:
            parts.extend([workspace_id, target_id, secret_name])
        encoded = "/".join(quote(part, safe="") for part in parts)
        return f"/v1/{self._mount}/data/{encoded}"

    def _metadata_path_for_scope(self, secret_name: str, tenant_scope: dict[str, str]) -> str:
        data_path = self._path_for_scope(secret_name, tenant_scope)
        return data_path.replace(f"/v1/{self._mount}/data/", f"/v1/{self._mount}/metadata/", 1)

    def _metadata_list_path(self, *parts: str) -> str:
        path_parts = self._path_prefix.split("/") if self._path_prefix else []
        path_parts.extend(parts)
        encoded = "/".join(quote(part, safe="") for part in path_parts)
        suffix = f"/{encoded}" if encoded else ""
        return f"/v1/{self._mount}/metadata{suffix}"

    async def _record_dependency_reachable(self, dependency_key: str) -> None:
        await dependency_circuit_breaker.record_success(dependency_key)

    async def get_secret(self, secret_name: str, tenant_scope: dict[str, str]) -> str:
        cache_key = (secret_name, json.dumps(tenant_scope, sort_keys=True))
        if self._cache_ttl > 0 and cache_key in self._cache:
            plaintext, expires_at = self._cache[cache_key]
            if time.time() < expires_at:
                return plaintext
            self._cache.pop(cache_key, None)

        dependency_key = "secret_backend:vault"
        for scope in self._candidate_scopes(tenant_scope):
            attempt = 1
            while attempt <= max(1, settings.VAULT_READ_RETRY_ATTEMPTS):
                try:
                    await dependency_circuit_breaker.before_call(
                        dependency_key,
                        "secret_backend",
                        "vault",
                    )
                    response = await self._client.get(
                        self._path_for_scope(secret_name, scope),
                        headers=self._headers(),
                    )
                    if response.status_code == 404:
                        await self._record_dependency_reachable(dependency_key)
                        break
                    response.raise_for_status()

                    body = response.json()
                    plaintext = body.get("data", {}).get("data", {}).get("value")
                    if not isinstance(plaintext, str):
                        raise RuntimeError("Vault secret payload missing string field 'value'")

                    if self._cache_ttl > 0:
                        self._cache[cache_key] = (
                            plaintext,
                            time.time() + self._cache_ttl,
                        )
                    await dependency_circuit_breaker.record_success(dependency_key)
                    return plaintext
                except CircuitOpenError:
                    raise
                except Exception as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                        await self._record_dependency_reachable(dependency_key)
                        break

                    note_dependency_event("secret_backend", "failure")
                    retryable = is_retryable_dependency_error(exc)
                    logger.warning(
                        "vault_secret_read_failed",
                        secret_name=secret_name,
                        scope=scope,
                        attempt=attempt,
                        max_attempts=settings.VAULT_READ_RETRY_ATTEMPTS,
                        error=str(exc),
                    )
                    if retryable:
                        opened = await dependency_circuit_breaker.record_failure(
                            dependency_key,
                            settings.OUTBOUND_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                            settings.OUTBOUND_CIRCUIT_BREAKER_RESET_MS,
                        )
                        if opened:
                            note_dependency_event("secret_backend", "circuit_open")
                        if attempt < settings.VAULT_READ_RETRY_ATTEMPTS and not opened:
                            note_dependency_event("secret_backend", "retry")
                            await asyncio.sleep(
                                backoff_seconds(settings.VAULT_READ_BACKOFF_MS, attempt)
                            )
                            attempt += 1
                            continue
                    raise

        raise SecretNotFoundError(f"Secret {secret_name} not found for the given scope")

    async def put_secret(
        self, secret_name: str, plaintext: str, tenant_scope: dict[str, str]
    ) -> None:
        payload = {"data": {"value": plaintext}}
        response = await self._client.post(
            self._path_for_scope(secret_name, tenant_scope),
            headers=self._headers(),
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Vault put failed ({response.status_code}): {response.text}"
            )

        self._evict_secret_cache(secret_name)
        await self._publish_secret_invalidation(secret_name)

    async def delete_secret(self, secret_name: str, tenant_scope: dict[str, str]) -> None:
        response = await self._client.delete(
            self._metadata_path_for_scope(secret_name, tenant_scope),
            headers=self._headers(),
        )
        if response.status_code not in (204, 404):
            response.raise_for_status()

        self._evict_secret_cache(secret_name)
        await self._publish_secret_invalidation(secret_name)

    async def _mcp_secret_names(
        self,
        workspace_id: str,
        *,
        server_id: str | None = None,
        owner_type: McpSecretOwnerType | None = None,
        owner_id: str | None = None,
    ) -> list[str]:
        response = await self._client.request(
            "LIST",
            self._metadata_list_path(workspace_id, "_global"),
            headers=self._headers(),
        )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise RuntimeError(
                f"Vault MCP secret inventory failed ({response.status_code})"
            )
        keys = response.json().get("data", {}).get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise RuntimeError("Vault MCP secret inventory returned invalid keys")
        return sorted(
            key
            for key in keys
            if not key.endswith("/")
            and matches_mcp_secret_name(
                key,
                workspace_id,
                server_id=server_id,
                owner_type=owner_type,
                owner_id=owner_id,
            )
        )

    async def count_mcp_secrets(
        self,
        workspace_id: str,
        *,
        server_id: str | None = None,
        owner_type: McpSecretOwnerType | None = None,
        owner_id: str | None = None,
    ) -> int:
        return len(
            await self._mcp_secret_names(
                workspace_id,
                server_id=server_id,
                owner_type=owner_type,
                owner_id=owner_id,
            )
        )

    async def purge_mcp_secrets(
        self,
        workspace_id: str,
        *,
        server_id: str | None = None,
        owner_type: McpSecretOwnerType | None = None,
        owner_id: str | None = None,
    ) -> int:
        names = await self._mcp_secret_names(
            workspace_id,
            server_id=server_id,
            owner_type=owner_type,
            owner_id=owner_id,
        )
        for name in names:
            await self.delete_secret(name, {"workspace_id": workspace_id})
        return len(names)

    async def _all_mcp_secret_locations(
        self, owner_type: McpSecretOwnerType
    ) -> list[tuple[str, str]]:
        """Inventory exact MCP identities in every Vault workspace."""

        response = await self._client.request(
            "LIST",
            self._metadata_list_path(),
            headers=self._headers(),
        )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise RuntimeError(
                f"Vault MCP workspace inventory failed ({response.status_code})"
            )
        keys = response.json().get("data", {}).get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise RuntimeError("Vault MCP workspace inventory returned invalid keys")
        locations: list[tuple[str, str]] = []
        for key in keys:
            if not key.endswith("/"):
                continue
            workspace_id = unquote(key[:-1])
            if not workspace_id or workspace_id == "_readiness":
                continue
            for name in await self._mcp_secret_names(
                workspace_id,
                owner_type=owner_type,
            ):
                locations.append((workspace_id, name))
        return locations

    async def count_all_mcp_user_secrets(self) -> int:
        return len(await self._all_mcp_secret_locations("user"))

    async def count_all_mcp_installation_secrets(self) -> int:
        return len(await self._all_mcp_secret_locations("installation"))

    async def purge_all_mcp_user_secrets(self) -> int:
        locations = await self._all_mcp_secret_locations("user")
        for workspace_id, name in locations:
            await self.delete_secret(name, {"workspace_id": workspace_id})
        return len(locations)

    async def _generated_catalog_secret_names(self, workspace_id: str) -> list[str]:
        response = await self._client.request(
            "LIST",
            self._metadata_list_path(workspace_id, "_global"),
            headers=self._headers(),
        )
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise RuntimeError(
                f"Vault catalog secret inventory failed ({response.status_code})"
            )
        keys = response.json().get("data", {}).get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise RuntimeError("Vault catalog secret inventory returned invalid keys")
        return sorted(
            key for key in keys if matches_generated_catalog_secret_name(key)
        )

    async def count_generated_catalog_secrets(self, workspace_id: str) -> int:
        return len(await self._generated_catalog_secret_names(workspace_id))

    async def purge_generated_catalog_secrets(self, workspace_id: str) -> int:
        names = await self._generated_catalog_secret_names(workspace_id)
        for name in names:
            await self.delete_secret(name, {"workspace_id": workspace_id})
        return len(names)

    async def health_check(self) -> None:
        response = await self._client.get("/v1/sys/health", headers=self._headers())
        if response.status_code not in {200, 429, 472, 473}:
            raise RuntimeError(f"Vault health check failed with status {response.status_code}")
        list_response = await self._client.request(
            "LIST",
            # Exercise the same path shape used by workspace inventory. A
            # parent-prefix LIST is neither sufficient nor necessary under
            # path-specific Vault policies.
            self._metadata_list_path("_readiness", "_global"),
            headers=self._headers(),
        )
        if list_response.status_code not in {200, 404}:
            raise RuntimeError(
                "Vault token must have list capability on the configured metadata prefix"
            )

    async def close(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        if self._pubsub is not None:
            await self._pubsub.close()
            self._pubsub = None
        if self._redis is not None:
            await self._redis.aclose()
        await self._client.aclose()

    def _evict_secret_cache(self, secret_name: str) -> None:
        for cache_key in list(self._cache):
            if cache_key[0] == secret_name:
                self._cache.pop(cache_key, None)

    async def _publish_secret_invalidation(self, secret_name: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.publish(
                SECRET_CACHE_INVALIDATION_CHANNEL,
                json.dumps({"secret_name": secret_name}),
            )
        except Exception as exc:
            logger.warning(
                "secret_cache_invalidation_publish_failed",
                secret_name=secret_name,
                error=str(exc),
            )

    async def start_cache_invalidation_listener(self) -> None:
        if self._redis is None or self._listener_task is not None:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(SECRET_CACHE_INVALIDATION_CHANNEL)
        self._listener_task = asyncio.create_task(self._listen_for_invalidations())

    async def _listen_for_invalidations(self) -> None:
        if self._pubsub is None:
            return
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                payload = json.loads(data)
                self._evict_secret_cache(payload["secret_name"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("secret_cache_invalidation_listener_failed", error=str(exc))
