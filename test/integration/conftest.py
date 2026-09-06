import pytest
import pytest_asyncio

from app.catalog.store import catalog_store
from app.config.settings import settings
from app.execution_capacity import execution_authority
from app.mcp.connections import mcp_connection_store
from app.mcp.lifecycle import mcp_lifecycle_store
from app.mcp.oauth.registration_store import oauth_registration_store
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.secrets.store import secret_store


@pytest.fixture(autouse=True)
def stub_control_plane_execution_authority(monkeypatch: pytest.MonkeyPatch):
    """Keep gateway integration tests focused when no control plane is provisioned."""
    if settings.WORKSPACE_CAPACITY_ENABLED:
        pytest.fail("Gateway integration tests require capacity to remain disabled")

    async def authorize(_claims) -> None:
        return None

    monkeypatch.setattr(execution_authority, "authorize", authorize)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def close_global_async_stores_after_test():
    """Prevent pooled connections from crossing pytest event-loop boundaries."""
    yield

    await catalog_store.close()
    await mcp_connection_store.close()
    await mcp_lifecycle_store.close()
    await mcp_server_registry.close()
    await tool_registry.close()
    await oauth_registration_store.close()
    close_secret_store = getattr(secret_store, "close", None)
    if close_secret_store is not None:
        await close_secret_store()
