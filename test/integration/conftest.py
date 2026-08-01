import pytest_asyncio

from app.catalog.store import catalog_store
from app.mcp.connections import mcp_connection_store
from app.mcp.oauth.registration_store import oauth_registration_store
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.secrets.store import secret_store


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def close_global_async_stores_after_test():
    """Prevent pooled connections from crossing pytest event-loop boundaries."""
    yield

    await catalog_store.close()
    await mcp_connection_store.close()
    await mcp_server_registry.close()
    await tool_registry.close()
    await oauth_registration_store.close()
    close_secret_store = getattr(secret_store, "close", None)
    if close_secret_store is not None:
        await close_secret_store()
