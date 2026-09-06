from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.mcp.connections import mcp_connection_store
from app.mcp.lifecycle import mcp_lifecycle_store


@pytest.fixture(autouse=True)
def isolate_global_mcp_lifecycle_store(monkeypatch, request: pytest.FixtureRequest):
    """Keep handler unit tests independent from the production lifecycle DB.

    Lifecycle-store tests instantiate their own SQLite store. Route tests that
    exercise fence failures override these fakes explicitly. Integration tests
    must exercise the real database-backed lifecycle boundary.
    """

    if "integration" in request.node.path.parts:
        return

    @asynccontextmanager
    async def destination_operation(_destination):
        yield

    @asynccontextmanager
    async def server_operation(
        _workspace_id,
        _server_id,
        *,
        expected_credential_epoch=None,
        allow_transitioning=False,
    ):
        del allow_transitioning
        yield SimpleNamespace(credential_epoch=expected_credential_epoch or 1)

    @asynccontextmanager
    async def mutation_lock(*_args, **_kwargs):
        yield

    async def assert_user_active(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp_lifecycle_store, "destination_operation", destination_operation)
    monkeypatch.setattr(mcp_lifecycle_store, "server_operation", server_operation)
    monkeypatch.setattr(mcp_lifecycle_store, "workspace_mutation_lock", mutation_lock)
    monkeypatch.setattr(mcp_lifecycle_store, "destination_mutation_lock", mutation_lock)
    monkeypatch.setattr(mcp_lifecycle_store, "server_mutation_lock", mutation_lock)
    monkeypatch.setattr(mcp_lifecycle_store, "assert_user_active", assert_user_active)
    monkeypatch.setattr(mcp_connection_store, "_supports_advisory_locks", False)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_execution_authority_http(monkeypatch, request):
    """Unit handlers use a deterministic CP transport; authority tests override it."""
    if "integration" in request.node.path.parts:
        return
    from app.config.settings import settings
    from app.execution_capacity import execution_authority

    async def post(_run_id, action, _payload):
        return {
            "status": "ok",
            "contractVersion": 1,
            **(
                {"capacityEnabled": settings.WORKSPACE_CAPACITY_ENABLED}
                if action == "authorize"
                else {}
            ),
        }

    monkeypatch.setattr(execution_authority, "_post", post)
