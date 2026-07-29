"""Encrypted, single-use OAuth preparation and callback state."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time

from pydantic import BaseModel
from redis.asyncio import Redis

from app.config.settings import settings
from app.mcp.oauth.errors import oauth_error
from app.mcp.oauth.models import OAuthFlowRecord, OAuthPreparationRecord
from app.outbound_tls import redis_tls_kwargs
from app.secrets.crypto import crypto

_PUT_RECORD_SCRIPT = """
if redis.call("EXISTS", KEYS[1]) == 1 then
  return 0
end
redis.call("SET", KEYS[1], ARGV[1], "EX", ARGV[2])
redis.call("SADD", KEYS[2], KEYS[1])
redis.call("EXPIRE", KEYS[2], ARGV[2])
return 1
"""


def _record_key(kind: str, handle: str) -> str:
    digest = hashlib.sha256(handle.encode()).hexdigest()
    return f"gateway:mcp:oauth:{kind}:{digest}"


def _aad(kind: str, handle: str) -> bytes:
    return f"mcp-oauth:{kind}:{hashlib.sha256(handle.encode()).hexdigest()}".encode()


def _connection_index_key(workspace_id: str, server_id: str, owner_id: str) -> str:
    material = f"{workspace_id}\0{server_id}\0{owner_id}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return f"gateway:mcp:oauth:index:{digest}"


def _record_index_key(record: OAuthPreparationRecord | OAuthFlowRecord) -> str:
    return _connection_index_key(
        record.workspace_id,
        record.server_id,
        record.owner_id,
    )


def _encrypt(kind: str, handle: str, record: BaseModel) -> str:
    plaintext = record.model_dump_json()
    ciphertext, nonce = crypto.encrypt(plaintext, _aad(kind, handle))
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def _decrypt[T: BaseModel](kind: str, handle: str, value: str, model: type[T]) -> T:
    try:
        raw = base64.urlsafe_b64decode(value.encode())
        plaintext = crypto.decrypt(raw[12:], raw[:12], _aad(kind, handle))
        return model.model_validate_json(plaintext)
    except Exception as exc:
        raise oauth_error(
            "MCP_OAUTH_FLOW_INVALID",
            "The OAuth request is invalid or expired.",
            status_code=400,
        ) from exc


class OAuthFlowStore:
    """Redis-backed flow state with a bounded development fallback."""

    def __init__(self) -> None:
        self._redis = (
            Redis.from_url(settings.REDIS_URL, **redis_tls_kwargs(settings.REDIS_URL))
            if settings.REDIS_URL
            else None
        )
        self._memory: dict[str, tuple[str, float]] = {}
        self._memory_indexes: dict[str, set[str]] = {}
        self._memory_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def _put(
        self,
        kind: str,
        handle: str,
        record: OAuthPreparationRecord | OAuthFlowRecord,
    ) -> None:
        key = _record_key(kind, handle)
        value = _encrypt(kind, handle, record)
        ttl = settings.MCP_OAUTH_FLOW_TTL_SECONDS
        index_key = _record_index_key(record)
        if self._redis is not None:
            created = await self._redis.eval(
                _PUT_RECORD_SCRIPT,
                2,
                key,
                index_key,
                value,
                ttl,
            )
            if not created:
                raise oauth_error(
                    "MCP_OAUTH_FLOW_INVALID",
                    "The OAuth request could not be initialized.",
                    status_code=409,
                )
            return
        async with self._memory_lock:
            if key in self._memory:
                raise oauth_error(
                    "MCP_OAUTH_FLOW_INVALID",
                    "The OAuth request could not be initialized.",
                    status_code=409,
                )
            self._memory[key] = (value, time.monotonic() + ttl)
            self._memory_indexes.setdefault(index_key, set()).add(key)

    async def _consume[T: BaseModel](
        self,
        kind: str,
        handle: str,
        model: type[T],
    ) -> T:
        key = _record_key(kind, handle)
        value: str | bytes | None
        if self._redis is not None:
            value = await self._redis.getdel(key)
        else:
            async with self._memory_lock:
                stored = self._memory.pop(key, None)
            value = (
                stored[0]
                if stored is not None and stored[1] >= time.monotonic()
                else None
            )
        if value is None:
            raise oauth_error(
                "MCP_OAUTH_FLOW_INVALID",
                "The OAuth request is invalid or expired.",
                status_code=400,
            )
        if isinstance(value, bytes):
            value = value.decode()
        record = _decrypt(kind, handle, value, model)
        index_key = _record_index_key(record)
        if self._redis is not None:
            await self._redis.srem(index_key, key)
        else:
            async with self._memory_lock:
                indexed = self._memory_indexes.get(index_key)
                if indexed is not None:
                    indexed.discard(key)
                    if not indexed:
                        self._memory_indexes.pop(index_key, None)
        return record

    async def create_preparation(self, record: OAuthPreparationRecord) -> str:
        handle = secrets.token_urlsafe(32)
        await self._put("preparation", handle, record)
        return handle

    async def consume_preparation(self, handle: str) -> OAuthPreparationRecord:
        return await self._consume(
            "preparation",
            handle,
            OAuthPreparationRecord,
        )

    async def create_flow(self, state: str, record: OAuthFlowRecord) -> None:
        await self._put("flow", state, record)

    async def consume_flow(self, state: str) -> OAuthFlowRecord:
        return await self._consume("flow", state, OAuthFlowRecord)

    async def delete_for_connection(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
    ) -> None:
        """Remove pending preparations and callback state for one user connection."""

        index_key = _connection_index_key(workspace_id, server_id, owner_id)
        if self._redis is not None:
            members = await self._redis.smembers(index_key)
            keys = [
                member.decode() if isinstance(member, bytes) else str(member)
                for member in members
            ]
            async with self._redis.pipeline(transaction=True) as pipeline:
                if keys:
                    pipeline.delete(*keys)
                pipeline.delete(index_key)
                await pipeline.execute()
            return
        async with self._memory_lock:
            keys = self._memory_indexes.pop(index_key, set())
            for key in keys:
                self._memory.pop(key, None)


oauth_flow_store = OAuthFlowStore()
