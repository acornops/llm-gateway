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
from app.mcp.identity import canonical_mcp_server_id
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
redis.call("SADD", KEYS[3], KEYS[1])
redis.call("SADD", KEYS[4], KEYS[1])
redis.call("SADD", KEYS[5], KEYS[1])
redis.call("SADD", KEYS[6], KEYS[2], KEYS[3], KEYS[4], KEYS[5])
redis.call("EXPIRE", KEYS[2], ARGV[2])
redis.call("EXPIRE", KEYS[3], ARGV[2])
redis.call("EXPIRE", KEYS[4], ARGV[2])
redis.call("EXPIRE", KEYS[5], ARGV[2])
redis.call("EXPIRE", KEYS[6], ARGV[2])
return 1
"""

_DELETE_INDEX_SCRIPT = """
local records = redis.call("SMEMBERS", KEYS[1])
for _, record_key in ipairs(records) do
  local reverse_key = record_key .. ":indexes"
  local indexes = redis.call("SMEMBERS", reverse_key)
  for _, index_key in ipairs(indexes) do
    redis.call("SREM", index_key, record_key)
  end
  redis.call("DEL", reverse_key)
  redis.call("DEL", record_key)
end
redis.call("DEL", KEYS[1])
return #records
"""


def _record_key(kind: str, handle: str) -> str:
    digest = hashlib.sha256(handle.encode()).hexdigest()
    return f"gateway:mcp:oauth:{kind}:{digest}"


def _aad(kind: str, handle: str) -> bytes:
    return f"mcp-oauth:{kind}:{hashlib.sha256(handle.encode()).hexdigest()}".encode()


def _record_reverse_index_key(record_key: str) -> str:
    return f"{record_key}:indexes"


def _connection_index_key(workspace_id: str, server_id: str, owner_id: str) -> str:
    server_id = canonical_mcp_server_id(server_id)
    material = f"{workspace_id}\0{server_id}\0{owner_id}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return f"gateway:mcp:oauth:index:{digest}"


def _server_index_key(workspace_id: str, server_id: str) -> str:
    server_id = canonical_mcp_server_id(server_id)
    material = f"{workspace_id}\0{server_id}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return f"gateway:mcp:oauth:server-index:{digest}"


def _workspace_index_key(workspace_id: str) -> str:
    digest = hashlib.sha256(workspace_id.encode()).hexdigest()
    return f"gateway:mcp:oauth:workspace-index:{digest}"


def _user_index_key(workspace_id: str, owner_id: str) -> str:
    material = f"{workspace_id}\0{owner_id}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return f"gateway:mcp:oauth:user-index:{digest}"


def _record_index_keys(record: OAuthPreparationRecord | OAuthFlowRecord) -> tuple[str, ...]:
    return (
        _connection_index_key(record.workspace_id, record.server_id, record.owner_id),
        _server_index_key(record.workspace_id, record.server_id),
        _workspace_index_key(record.workspace_id),
        _user_index_key(record.workspace_id, record.owner_id),
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
        index_keys = _record_index_keys(record)
        if self._redis is not None:
            created = await self._redis.eval(
                _PUT_RECORD_SCRIPT,
                6,
                key,
                *index_keys,
                _record_reverse_index_key(key),
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
            for index_key in index_keys:
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
                if stored is not None and stored[1] < time.monotonic():
                    self._remove_memory_index_memberships({key})
            value = stored[0] if stored is not None and stored[1] >= time.monotonic() else None
        if value is None:
            raise oauth_error(
                "MCP_OAUTH_FLOW_INVALID",
                "The OAuth request is invalid or expired.",
                status_code=400,
            )
        if isinstance(value, bytes):
            value = value.decode()
        record = _decrypt(kind, handle, value, model)
        index_keys = _record_index_keys(record)
        if self._redis is not None:
            async with self._redis.pipeline(transaction=True) as pipeline:
                for index_key in index_keys:
                    pipeline.srem(index_key, key)
                pipeline.delete(_record_reverse_index_key(key))
                await pipeline.execute()
        else:
            async with self._memory_lock:
                for index_key in index_keys:
                    indexed = self._memory_indexes.get(index_key)
                    if indexed is not None:
                        indexed.discard(key)
                        if not indexed:
                            self._memory_indexes.pop(index_key, None)
        return record

    async def _get[T: BaseModel](self, kind: str, handle: str, model: type[T]) -> T:
        """Read bounded flow metadata without consuming callback state."""

        key = _record_key(kind, handle)
        value: str | bytes | None
        if self._redis is not None:
            value = await self._redis.get(key)
        else:
            async with self._memory_lock:
                stored = self._memory.get(key)
                if stored is not None and stored[1] < time.monotonic():
                    self._memory.pop(key, None)
                    self._remove_memory_index_memberships({key})
            value = stored[0] if stored is not None and stored[1] >= time.monotonic() else None
        if value is None:
            raise oauth_error(
                "MCP_OAUTH_FLOW_INVALID",
                "The OAuth request is invalid or expired.",
                status_code=400,
            )
        if isinstance(value, bytes):
            value = value.decode()
        return _decrypt(kind, handle, value, model)

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

    async def get_preparation(self, handle: str) -> OAuthPreparationRecord:
        return await self._get("preparation", handle, OAuthPreparationRecord)

    async def create_flow(self, state: str, record: OAuthFlowRecord) -> None:
        await self._put("flow", state, record)

    async def consume_flow(self, state: str) -> OAuthFlowRecord:
        return await self._consume("flow", state, OAuthFlowRecord)

    async def get_flow(self, state: str) -> OAuthFlowRecord:
        return await self._get("flow", state, OAuthFlowRecord)

    async def delete_for_connection(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
    ) -> None:
        """Remove pending preparations and callback state for one user connection."""

        await self._delete_index(_connection_index_key(workspace_id, server_id, owner_id))

    async def delete_for_server(self, workspace_id: str, server_id: str) -> None:
        await self._delete_index(_server_index_key(workspace_id, server_id))

    async def delete_for_workspace(self, workspace_id: str) -> None:
        await self._delete_index(_workspace_index_key(workspace_id))

    async def delete_for_user(self, workspace_id: str, owner_id: str) -> None:
        await self._delete_index(_user_index_key(workspace_id, owner_id))

    async def count_all_flow_records(self) -> int:
        """Count every bounded OAuth preparation, callback, and index key."""

        if self._redis is not None:
            count = 0
            async for _key in self._redis.scan_iter(match="gateway:mcp:oauth:*", count=500):
                count += 1
            return count
        async with self._memory_lock:
            now = time.monotonic()
            expired = {
                key for key, (_value, expires_at) in self._memory.items() if expires_at < now
            }
            for key in expired:
                self._memory.pop(key, None)
            self._remove_memory_index_memberships(expired)
            return len(self._memory) + len(self._memory_indexes)

    async def purge_all_flow_state(self) -> int:
        """Remove every individual OAuth flow/index during the pinned reset."""

        before = await self.count_all_flow_records()
        if self._redis is not None:
            batch: list[str | bytes] = []
            async for key in self._redis.scan_iter(
                match="gateway:mcp:oauth:*",
                count=500,
            ):
                batch.append(key)
                if len(batch) >= 500:
                    await self._redis.delete(*batch)
                    batch.clear()
            if batch:
                await self._redis.delete(*batch)
            return before
        async with self._memory_lock:
            self._memory.clear()
            self._memory_indexes.clear()
        return before

    async def _delete_index(self, index_key: str) -> None:
        if self._redis is not None:
            await self._redis.eval(_DELETE_INDEX_SCRIPT, 1, index_key)
            return
        async with self._memory_lock:
            keys = self._memory_indexes.pop(index_key, set())
            for key in keys:
                self._memory.pop(key, None)
            self._remove_memory_index_memberships(keys)

    def _remove_memory_index_memberships(self, keys: set[str]) -> None:
        for existing_index, members in list(self._memory_indexes.items()):
            removed_membership = bool(members.intersection(keys))
            members.difference_update(keys)
            if removed_membership and not members:
                self._memory_indexes.pop(existing_index, None)


oauth_flow_store = OAuthFlowStore()
