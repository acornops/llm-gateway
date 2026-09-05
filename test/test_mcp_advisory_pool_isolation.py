import pytest

from app.mcp.connections import McpConnectionStore
from app.mcp.lifecycle import McpLifecycleStore
from app.mcp.oauth.registration_store import OAuthRegistrationStore


@pytest.mark.anyio
async def test_postgresql_advisory_locks_use_pools_separate_from_state_queries() -> None:
    database_url = "postgresql+asyncpg://gateway:password@postgres/gateway"
    lifecycle = McpLifecycleStore(database_url)
    connections = McpConnectionStore(database_url)
    registrations = OAuthRegistrationStore(database_url)
    try:
        assert lifecycle._supports_advisory_locks is True
        assert lifecycle._advisory_lock_engine is not lifecycle.engine
        assert lifecycle._session_lock_engine is not lifecycle.engine
        assert lifecycle._session_lock_engine is not lifecycle._advisory_lock_engine
        assert lifecycle._advisory_lock_engine.pool.size() == 5
        assert lifecycle._session_lock_engine.pool.size() == 2

        assert connections._supports_advisory_locks is True
        assert connections._advisory_lock_engine is not connections.engine
        assert connections._advisory_lock_engine.pool.size() == 5

        assert registrations._supports_advisory_locks is True
        assert registrations._advisory_lock_engine is not registrations.engine
        assert registrations._advisory_lock_engine.pool.size() == 2
    finally:
        await lifecycle.close()
        await connections.close()
        await registrations.close()
