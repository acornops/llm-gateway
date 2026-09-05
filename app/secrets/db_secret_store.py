import asyncio
import contextlib
import json
import time

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, desc, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.outbound_tls import redis_tls_kwargs, sqlalchemy_connection_config
from app.secrets.crypto import crypto
from app.secrets.db_models import Secret
from app.secrets.errors import SecretNotFoundError
from app.secrets.interface import SecretStore
from app.secrets.mcp_names import (
    McpSecretOwnerType,
    matches_generated_catalog_secret_name,
    matches_mcp_secret_name,
)

logger = structlog.get_logger()
SECRET_CACHE_INVALIDATION_CHANNEL = "gateway:secret-cache-invalidation"


class DbSecretStore(SecretStore):
    """
    Postgres-backed secret store with AES-256-GCM encryption and in-memory caching.
    """

    def __init__(self, database_url: str):
        database_url, connect_args = sqlalchemy_connection_config(database_url)
        self.engine = create_async_engine(
            database_url,
            connect_args=connect_args,
        )
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._cache = {}  # (secret_name, tenant_scope_str) -> (plaintext, expires_at)
        self._cache_ttl = settings.SECRETS_CACHE_TTL_SEC
        self._redis = (
            Redis.from_url(settings.REDIS_URL, **redis_tls_kwargs(settings.REDIS_URL))
            if settings.REDIS_URL
            else None
        )
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None

    def _validate_scope(self, tenant_scope: dict[str, str]) -> None:
        has_target_id = "target_id" in tenant_scope
        has_target_type = "target_type" in tenant_scope
        if has_target_id != has_target_type:
            raise ValueError("Secret target scope requires both target_id and target_type")

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

    async def get_secret(self, secret_name: str, tenant_scope: dict[str, str]) -> str:
        """
        Retrieves a secret from cache or database.
        """
        self._validate_scope(tenant_scope)
        cache_key = (secret_name, json.dumps(tenant_scope, sort_keys=True))
        if self._cache_ttl > 0 and cache_key in self._cache:
            plaintext, expires_at = self._cache[cache_key]
            if time.time() < expires_at:
                return plaintext
            self._cache.pop(cache_key, None)

        async with self.async_session() as session:
            secret_record = None
            for scope in self._candidate_scopes(tenant_scope):
                stmt = (
                    select(Secret)
                    .where(Secret.secret_name == secret_name, Secret.tenant_scope == scope)
                    .order_by(desc(Secret.version), desc(Secret.created_at))
                )
                result = await session.execute(stmt)
                secret_record = result.scalars().first()
                if secret_record:
                    break

            if not secret_record:
                raise SecretNotFoundError(
                    f"Secret {secret_name} not found for the given scope"
                )

            plaintext = crypto.decrypt(
                secret_record.ciphertext, secret_record.nonce, secret_record.aad
            )

            if self._cache_ttl > 0:
                self._cache[cache_key] = (plaintext, time.time() + self._cache_ttl)
            return plaintext

    async def close(self):
        """Closes the underlying database engine."""
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
        await self.engine.dispose()

    async def health_check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def put_secret(
        self, secret_name: str, plaintext: str, tenant_scope: dict[str, str]
    ) -> None:
        self._validate_scope(tenant_scope)
        aad = f"{secret_name}:{json.dumps(tenant_scope, sort_keys=True)}".encode()
        ciphertext, nonce = crypto.encrypt(plaintext, aad)

        async with self.async_session() as session:
            stmt = (
                select(Secret)
                .where(Secret.secret_name == secret_name, Secret.tenant_scope == tenant_scope)
                .order_by(desc(Secret.version), desc(Secret.created_at))
            )
            result = await session.execute(stmt)
            existing = result.scalars().first()

            if existing:
                existing.ciphertext = ciphertext
                existing.nonce = nonce
                existing.aad = aad
                existing.version = (existing.version or 1) + 1
                existing.key_id = "v1"
            else:
                secret_record = Secret(
                    tenant_scope=tenant_scope,
                    secret_name=secret_name,
                    ciphertext=ciphertext,
                    nonce=nonce,
                    aad=aad,
                    key_id="v1",
                    version=1,
                )
                session.add(secret_record)
            await session.commit()

        self._evict_secret_cache(secret_name)
        await self._publish_secret_invalidation(secret_name)

    async def delete_secret(self, secret_name: str, tenant_scope: dict[str, str]) -> None:
        self._validate_scope(tenant_scope)
        async with self.async_session() as session:
            await session.execute(
                delete(Secret).where(
                    Secret.secret_name == secret_name,
                    Secret.tenant_scope == tenant_scope,
                )
            )
            await session.commit()

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
        scope = {"workspace_id": workspace_id}
        async with self.async_session() as session:
            names = list(
                (
                    await session.execute(
                        select(Secret.secret_name).where(Secret.tenant_scope == scope)
                    )
                ).scalars()
            )
        return sorted(
            {
                name
                for name in names
                if matches_mcp_secret_name(
                    name,
                    workspace_id,
                    server_id=server_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                )
            }
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
        if not names:
            return 0
        scope = {"workspace_id": workspace_id}
        async with self.async_session() as session:
            await session.execute(
                delete(Secret).where(
                    Secret.tenant_scope == scope,
                    Secret.secret_name.in_(names),
                )
            )
            await session.commit()
        for name in names:
            self._evict_secret_cache(name)
            await self._publish_secret_invalidation(name)
        return len(names)

    async def _all_mcp_secret_rows(
        self, owner_type: McpSecretOwnerType
    ) -> list[tuple[object, str]]:
        """Inventory exact MCP identities across workspace scopes."""

        async with self.async_session() as session:
            rows = list(
                (
                    await session.execute(
                        select(Secret.id, Secret.tenant_scope, Secret.secret_name)
                    )
                ).all()
            )
        matched: list[tuple[object, str]] = []
        for secret_id, scope, name in rows:
            workspace_id = scope.get("workspace_id") if isinstance(scope, dict) else None
            if (
                isinstance(workspace_id, str)
                and isinstance(name, str)
                and matches_mcp_secret_name(
                    name,
                    workspace_id,
                    owner_type=owner_type,
                )
            ):
                matched.append((secret_id, name))
        return matched

    async def count_all_mcp_user_secrets(self) -> int:
        return len(await self._all_mcp_secret_rows("user"))

    async def count_all_mcp_installation_secrets(self) -> int:
        return len(await self._all_mcp_secret_rows("installation"))

    async def purge_all_mcp_user_secrets(self) -> int:
        rows = await self._all_mcp_secret_rows("user")
        if not rows:
            return 0
        secret_ids = [secret_id for secret_id, _ in rows]
        async with self.async_session() as session:
            for offset in range(0, len(secret_ids), 500):
                await session.execute(
                    delete(Secret).where(
                        Secret.id.in_(secret_ids[offset : offset + 500])
                    )
                )
            await session.commit()
        for _secret_id, name in rows:
            self._evict_secret_cache(name)
            await self._publish_secret_invalidation(name)
        return len(rows)

    async def _generated_catalog_secret_names(self, workspace_id: str) -> list[str]:
        scope = {"workspace_id": workspace_id}
        async with self.async_session() as session:
            names = list(
                (
                    await session.execute(
                        select(Secret.secret_name).where(Secret.tenant_scope == scope)
                    )
                ).scalars()
            )
        return sorted({name for name in names if matches_generated_catalog_secret_name(name)})

    async def count_generated_catalog_secrets(self, workspace_id: str) -> int:
        return len(await self._generated_catalog_secret_names(workspace_id))

    async def purge_generated_catalog_secrets(self, workspace_id: str) -> int:
        names = await self._generated_catalog_secret_names(workspace_id)
        if not names:
            return 0
        scope = {"workspace_id": workspace_id}
        async with self.async_session() as session:
            await session.execute(
                delete(Secret).where(
                    Secret.tenant_scope == scope,
                    Secret.secret_name.in_(names),
                )
            )
            await session.commit()
        for name in names:
            self._evict_secret_cache(name)
            await self._publish_secret_invalidation(name)
        return len(names)

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
