import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.mcp_admin_schemas import McpUserLifecycleRequest
from app.config.settings import settings
from app.main import app
from app.mcp.lifecycle import (
    McpDestination,
    McpLifecycleFencedError,
    McpLifecycleStore,
    McpUserLifecycleStaleError,
    mcp_lifecycle_store,
)
from app.mcp.oauth.errors import McpOAuthError
from app.mcp.oauth.flow_store import OAuthFlowStore
from app.mcp.oauth.models import OAuthEndpointSnapshot, OAuthPreparationRecord
from app.mcp.registry.models import McpServer, McpUserLifecycle
from app.mcp.user_lifecycle_contract import MAX_MCP_MEMBERSHIP_GENERATION
from app.secrets.db_models import Base


@pytest.fixture(autouse=True)
def mock_mcp_secret_purge():
    with (
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.purge_mcp_secrets",
            new=AsyncMock(return_value=0),
        ) as purge,
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.count_mcp_secrets",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.purge_generated_catalog_secrets",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.count_generated_catalog_secrets",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.delete_secret",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.list_sources",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.delete_workspace_sources",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.count_workspace_sources",
            new=AsyncMock(return_value=0),
        ),
    ):
        yield purge


async def _sqlite_lifecycle_store(tmp_path) -> McpLifecycleStore:
    store = McpLifecycleStore(f"sqlite+aiosqlite:///{tmp_path / f'lifecycle-{uuid.uuid4()}.db'}")
    async with store.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return store


async def _insert_target_server(store: McpLifecycleStore) -> McpServer:
    server = McpServer(
        id=uuid.uuid4(),
        workspace_id="ws-1",
        scope_type="target",
        target_id="cluster-a",
        target_type="kubernetes",
        server_name="remote",
        server_url="https://mcp.example.test/mcp",
        enabled=True,
        auth_type="none",
        credential_mode="none",
        provenance_type="manual",
        credential_epoch=1,
    )
    async with store.async_session() as session:
        session.add(server)
        await session.commit()
        await session.refresh(server)
    return server


@pytest.mark.anyio
async def test_destination_fence_is_terminal_and_blocks_server_operations(tmp_path) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    assert store._supports_advisory_locks is False
    try:
        server = await _insert_target_server(store)
        destination = McpDestination(
            workspace_id="ws-1",
            scope_type="target",
            destination_id="cluster-a",
            target_type="kubernetes",
        )
        async with store.destination_operation(destination):
            pass
        async with (
            store.workspace_mutation_lock("ws-1"),
            store.destination_mutation_lock(destination),
        ):
            first = await store.activate_destination_fence(destination)
            second = await store.activate_destination_fence(destination)
        assert first.fence_key == second.fence_key

        with pytest.raises(McpLifecycleFencedError):
            async with store.destination_operation(destination):
                pass
        with pytest.raises(McpLifecycleFencedError):
            async with store.server_operation("ws-1", str(server.id)):
                pass
    finally:
        await store.close()


@pytest.mark.anyio
async def test_create_started_before_teardown_finishes_then_future_creates_are_fenced(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    destination = McpDestination(
        workspace_id="ws-1",
        scope_type="target",
        destination_id="cluster-a",
        target_type="kubernetes",
    )
    create_entered = asyncio.Event()
    release_create = asyncio.Event()
    create_committed: list[bool] = []

    async def create_operation() -> None:
        async with store.destination_operation(destination):
            create_entered.set()
            await release_create.wait()
            create_committed.append(True)

    async def teardown_operation() -> None:
        async with (
            store.workspace_mutation_lock("ws-1"),
            store.destination_mutation_lock(destination),
        ):
            await store.activate_destination_fence(destination)

    try:
        create_task = asyncio.create_task(create_operation())
        await create_entered.wait()
        teardown_task = asyncio.create_task(teardown_operation())
        await asyncio.sleep(0)
        assert not teardown_task.done()
        release_create.set()
        await asyncio.gather(create_task, teardown_task)
        assert create_committed == [True]
        with pytest.raises(McpLifecycleFencedError):
            async with store.destination_operation(destination):
                pass
    finally:
        await store.close()


@pytest.mark.anyio
async def test_user_lifecycle_row_is_the_strict_rollout_gate(tmp_path) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    try:
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", None)
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", 1)

        async with store.workspace_mutation_lock("ws-1"):
            result = await store.reconcile_user_lifecycle("ws-1", "user-1", 1, "active")
        assert result == "activation_required"
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", None)
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", 1)

        async with store.workspace_mutation_lock("ws-1"):
            await store.complete_user_activation("ws-1", "user-1", 1)
        assert (await store.assert_user_active("ws-1", "user-1", 1)).status == "active"

        async with store.workspace_mutation_lock("ws-1"):
            assert await store.reconcile_user_lifecycle("ws-1", "user-1", 2, "removed") == "applied"
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", 1)

        async with store.workspace_mutation_lock("ws-1"):
            assert (
                await store.reconcile_user_lifecycle("ws-1", "user-1", 3, "active")
                == "activation_required"
            )
            await store.complete_user_activation("ws-1", "user-1", 3)
        assert (await store.assert_user_active("ws-1", "user-1", 3)).status == "active"
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", 1)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_first_active_reconcile_resets_individual_state_exactly_once(
    tmp_path,
    mock_mcp_secret_purge: AsyncMock,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    list_servers = AsyncMock(return_value=[SimpleNamespace(id="server-1")])
    cleanup = AsyncMock(return_value=True)
    delete_flows = AsyncMock()
    try:
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=list_servers,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.cleanup_user_server_connection",
                new=cleanup,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_user",
                new=delete_flows,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_user",
                new=AsyncMock(return_value=[]),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                responses = [
                    await client.put(
                        "/api/v1/internal/mcp/users/user-1/lifecycle",
                        headers={"Authorization": "Bearer dev_orchestrator_token"},
                        json={
                            "workspace_id": "ws-1",
                            "membership_generation": 1,
                            "status": "active",
                        },
                    )
                    for _ in range(2)
                ]

        assert [response.status_code for response in responses] == [204, 204]
        list_servers.assert_awaited_once_with("ws-1")
        cleanup.assert_awaited_once_with(
            "ws-1",
            "server-1",
            "user-1",
            reason="member_reactivation",
        )
        delete_flows.assert_awaited_once_with("ws-1", "user-1")
        mock_mcp_secret_purge.assert_awaited_once_with(
            "ws-1",
            owner_type="user",
            owner_id="user-1",
        )
        assert (await store.assert_user_active("ws-1", "user-1", 1)).status == "active"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_owner_reconcile_cleans_connection_inserted_after_initial_zero_state(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    server_lock_requested = asyncio.Event()
    late_insert_finished = asyncio.Event()
    connection_exists = False

    @asynccontextmanager
    async def wait_for_inflight_server_operation(_workspace_id, _server_id):
        server_lock_requested.set()
        await late_insert_finished.wait()
        yield

    async def cleanup(_workspace_id, _server_id, _user_id, *, reason):
        nonlocal connection_exists
        assert reason == "member_reactivation"
        assert connection_exists
        connection_exists = False
        return True

    async def residual_connections(_workspace_id, _user_id):
        return [SimpleNamespace()] if connection_exists else []

    try:
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch.object(
                store,
                "server_mutation_lock",
                new=wait_for_inflight_server_operation,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=AsyncMock(return_value=[SimpleNamespace(id="server-1")]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.cleanup_user_server_connection",
                new=AsyncMock(side_effect=cleanup),
            ) as cleanup_mock,
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_user",
                new=AsyncMock(),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_user",
                new=AsyncMock(side_effect=residual_connections),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                request_task = asyncio.create_task(
                    client.put(
                        "/api/v1/internal/mcp/users/user-1/lifecycle",
                        headers={"Authorization": "Bearer dev_orchestrator_token"},
                        json={
                            "workspace_id": "ws-1",
                            "membership_generation": 1,
                            "status": "active",
                        },
                    )
                )
                await server_lock_requested.wait()
                # This represents a mutation that cleared the workspace handoff
                # before reconciliation and commits only when its server lock is
                # released. The lifecycle sweep must re-read under that lock.
                connection_exists = True
                late_insert_finished.set()
                response = await request_task

        assert response.status_code == 204
        cleanup_mock.assert_awaited_once()
        assert connection_exists is False
        assert (await store.assert_user_active("ws-1", "user-1", 1)).status == "active"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_removed_retry_resumes_cleanup_after_failure(tmp_path) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    cleanup = AsyncMock(side_effect=[RuntimeError("secret backend unavailable"), True])
    try:
        with (
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_lifecycle_store",
                store,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=AsyncMock(return_value=[SimpleNamespace(id="server-1")]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.cleanup_user_server_connection",
                cleanup,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_user",
                new=AsyncMock(),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_user",
                new=AsyncMock(return_value=[]),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                first = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 2,
                        "status": "removed",
                    },
                )
                second = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 2,
                        "status": "removed",
                    },
                )

        assert first.status_code == 503
        assert first.json()["detail"]["code"] == "MCP_USER_LIFECYCLE_TEARDOWN_FAILED"
        assert second.status_code == 204
        assert cleanup.await_count == 2
        async with store.async_session() as session:
            row = await session.get(McpUserLifecycle, ("ws-1", "user-1"))
        assert row is not None and row.status == "removed"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_partial_removal_cleanup_is_drained_before_higher_generation_readd(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    cleanup = AsyncMock(side_effect=[RuntimeError("partial cleanup"), True])
    try:
        async with store.workspace_mutation_lock("ws-1"):
            await store.reconcile_user_lifecycle("ws-1", "user-1", 1, "active")
            await store.complete_user_activation("ws-1", "user-1", 1)
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=AsyncMock(return_value=[SimpleNamespace(id="server-1")]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.cleanup_user_server_connection",
                cleanup,
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_user",
                new=AsyncMock(),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_user",
                new=AsyncMock(return_value=[]),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                removed = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 2,
                        "status": "removed",
                    },
                )
                readded = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 3,
                        "status": "active",
                    },
                )

        assert removed.status_code == 503
        assert readded.status_code == 204
        assert cleanup.await_count == 2
        assert (await store.assert_user_active("ws-1", "user-1", 3)).status == "active"
        with pytest.raises(McpUserLifecycleStaleError):
            await store.assert_user_active("ws-1", "user-1", 1)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_user_generation_sweeps_serialize_without_holding_workspace_lock(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    first_sweep_started = asyncio.Event()
    release_first_sweep = asyncio.Event()
    cleanup_count = 0

    async def cleanup(*_args, **_kwargs):
        nonlocal cleanup_count
        cleanup_count += 1
        if cleanup_count == 1:
            first_sweep_started.set()
            await release_first_sweep.wait()
        return False

    try:
        async with store.workspace_mutation_lock("ws-1"):
            await store.reconcile_user_lifecycle("ws-1", "user-1", 1, "active")
            await store.complete_user_activation("ws-1", "user-1", 1)
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=AsyncMock(return_value=[SimpleNamespace(id="server-1")]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.cleanup_user_server_connection",
                new=AsyncMock(side_effect=cleanup),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_user",
                new=AsyncMock(),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_user",
                new=AsyncMock(return_value=[]),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                removal = asyncio.create_task(
                    client.put(
                        "/api/v1/internal/mcp/users/user-1/lifecycle",
                        headers={"Authorization": "Bearer dev_orchestrator_token"},
                        json={
                            "workspace_id": "ws-1",
                            "membership_generation": 2,
                            "status": "removed",
                        },
                    )
                )
                await first_sweep_started.wait()

                # The external cleanup is still blocked, but unrelated work can
                # acquire workspace authority after the short staging phase.
                async with asyncio.timeout(0.2):
                    async with store.workspace_mutation_lock("ws-1"):
                        pass

                readd = asyncio.create_task(
                    client.put(
                        "/api/v1/internal/mcp/users/user-1/lifecycle",
                        headers={"Authorization": "Bearer dev_orchestrator_token"},
                        json={
                            "workspace_id": "ws-1",
                            "membership_generation": 3,
                            "status": "active",
                        },
                    )
                )
                await asyncio.sleep(0.02)
                assert not readd.done()
                async with store.async_session() as session:
                    staged = await session.get(
                        McpUserLifecycle,
                        ("ws-1", "user-1"),
                    )
                assert staged is not None
                assert (staged.membership_generation, staged.status) == (2, "removed")

                release_first_sweep.set()
                removed_response, readded_response = await asyncio.gather(removal, readd)

        assert removed_response.status_code == 204
        assert readded_response.status_code == 204
        assert cleanup_count == 2
        assert (await store.assert_user_active("ws-1", "user-1", 3)).status == "active"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_user_lifecycle_session_lock_serializes_across_gateway_instances(
    tmp_path,
) -> None:
    stores = [await _sqlite_lifecycle_store(tmp_path) for _ in range(2)]
    advisory_locks: dict[int, asyncio.Lock] = {}
    connections: list[SimpleNamespace] = []

    class FakeConnection:
        def __init__(self) -> None:
            self.held: set[int] = set()
            self.closed = False

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_advisory_unlock_all" in sql:
                for held_key in list(self.held):
                    self.held.remove(held_key)
                    advisory_locks[held_key].release()
                return Mock()
            key = int(params["lock_key"])
            lock = advisory_locks.setdefault(key, asyncio.Lock())
            await lock.acquire()
            self.held.add(key)
            return Mock()

        async def commit(self) -> None:
            pass

        async def invalidate(self) -> None:
            for held_key in list(self.held):
                self.held.remove(held_key)
                advisory_locks[held_key].release()

        async def close(self) -> None:
            self.closed = True

    def new_connection() -> FakeConnection:
        connection = FakeConnection()
        connections.append(connection)
        return connection

    entered_first = asyncio.Event()
    entered_second = asyncio.Event()
    release_first = asyncio.Event()

    async def first_operation() -> None:
        async with stores[0].user_lifecycle_operation("ws-1", "user-1"):
            entered_first.set()
            await release_first.wait()

    async def second_operation() -> None:
        async with stores[1].user_lifecycle_operation("ws-1", "user-1"):
            entered_second.set()

    try:
        for store in stores:
            store._supports_advisory_locks = True
            store._session_lock_engine = SimpleNamespace(
                connect=AsyncMock(side_effect=new_connection),
                dispose=AsyncMock(),
            )

        first_task = asyncio.create_task(first_operation())
        await entered_first.wait()
        second_task = asyncio.create_task(second_operation())
        await asyncio.sleep(0.02)
        assert not entered_second.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert entered_second.is_set()
        assert len(connections) == 2
        assert all(connection.closed for connection in connections)
        assert all(not connection.held for connection in connections)
    finally:
        for store in stores:
            await store.close()


@pytest.mark.anyio
async def test_user_lifecycle_session_lock_cleanup_is_shielded_from_cancellation(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    connection = SimpleNamespace()

    async def execute(statement, _params=None):
        if "pg_advisory_unlock_all" in str(statement):
            cleanup_started.set()
            await allow_cleanup.wait()
        return Mock()

    connection.execute = AsyncMock(side_effect=execute)
    connection.commit = AsyncMock()
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    store._supports_advisory_locks = True
    store._session_lock_engine = SimpleNamespace(
        connect=AsyncMock(return_value=connection),
        dispose=AsyncMock(),
    )

    async def operate() -> None:
        async with store.user_lifecycle_operation("ws-1", "user-1"):
            entered.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(operate())
        await entered.wait()
        task.cancel()
        await cleanup_started.wait()
        assert not task.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        connection.invalidate.assert_not_awaited()
        connection.close.assert_awaited_once()
        assert store._locks == {}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_workspace_fence_rejects_delayed_user_reconcile_without_recreation(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    try:
        async with store.workspace_mutation_lock("ws-1"):
            await store.activate_workspace_fence("ws-1")
        with patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 1,
                        "status": "active",
                    },
                )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "MCP_LIFECYCLE_FENCED"
        async with store.async_session() as session:
            assert await session.get(McpUserLifecycle, ("ws-1", "user-1")) is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_user_lifecycle_rejects_lower_and_equal_conflicting_generations(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    try:
        async with store.workspace_mutation_lock("ws-1"):
            await store.reconcile_user_lifecycle("ws-1", "user-1", 2, "active")
            await store.complete_user_activation("ws-1", "user-1", 2)
        with patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                lower = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 1,
                        "status": "removed",
                    },
                )
                conflict = await client.put(
                    "/api/v1/internal/mcp/users/user-1/lifecycle",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                    json={
                        "workspace_id": "ws-1",
                        "membership_generation": 2,
                        "status": "removed",
                    },
                )
        assert lower.status_code == 409
        assert lower.json() == {
            "detail": {
                "code": "MCP_USER_LIFECYCLE_STALE",
                "message": (
                    "The workspace membership generation is older than the gateway lifecycle state."
                ),
                "retryable": False,
            }
        }
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == {
            "code": "MCP_USER_LIFECYCLE_CONFLICT",
            "message": (
                "The workspace membership generation already has a different lifecycle state."
            ),
            "retryable": False,
        }
    finally:
        await store.close()


def test_membership_generation_uses_strict_javascript_safe_integer_bounds() -> None:
    accepted = McpUserLifecycleRequest(
        workspace_id="ws-1",
        membership_generation=MAX_MCP_MEMBERSHIP_GENERATION,
        status="active",
    )
    assert accepted.membership_generation == 9_007_199_254_740_991

    for invalid in (
        0,
        MAX_MCP_MEMBERSHIP_GENERATION + 1,
        True,
        1.0,
        "1",
    ):
        with pytest.raises(ValidationError):
            McpUserLifecycleRequest(
                workspace_id="ws-1",
                membership_generation=invalid,
                status="active",
            )


@pytest.mark.anyio
async def test_workspace_teardown_deletes_owner_lifecycle_rows_after_cleanup(
    tmp_path,
    mock_mcp_secret_purge: AsyncMock,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    try:
        async with store.workspace_mutation_lock("ws-1"):
            await store.reconcile_user_lifecycle("ws-1", "user-1", 1, "active")
            await store.complete_user_activation("ws-1", "user-1", 1)
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_workspace",
                new=AsyncMock(),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_workspace",
                new=AsyncMock(return_value=[]),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.delete(
                    "/api/v1/internal/mcp/workspaces/ws-1",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                )
        assert response.status_code == 204
        mock_mcp_secret_purge.assert_awaited_once_with("ws-1")
        async with store.async_session() as session:
            assert await session.get(McpUserLifecycle, ("ws-1", "user-1")) is None
        with pytest.raises(McpLifecycleFencedError):
            await store.assert_workspace_not_fenced("ws-1")
    finally:
        await store.close()


@pytest.mark.anyio
async def test_workspace_teardown_deletes_all_catalog_secret_cursors_before_rows() -> None:
    source = SimpleNamespace(
        auth_secret_name="shared-catalog-reference",
        previous_auth_secret_name=("catalog_source::aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )
    delete_secret = AsyncMock()
    delete_sources = AsyncMock(return_value=1)
    with (
        patch.object(mcp_lifecycle_store, "activate_workspace_fence", new=AsyncMock()),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_workspace",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.list_sources",
            new=AsyncMock(return_value=[(source, [])]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.delete_secret",
            new=delete_secret,
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.delete_workspace_sources",
            new=delete_sources,
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.count_workspace_sources",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_workspace",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            mcp_lifecycle_store,
            "delete_user_lifecycles_for_workspace",
            new=AsyncMock(return_value=0),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(
                "/api/v1/internal/mcp/workspaces/ws-catalog",
                headers={"Authorization": "Bearer dev_orchestrator_token"},
            )

    assert response.status_code == 204
    assert {call.args[0] for call in delete_secret.await_args_list} == {
        "shared-catalog-reference",
        "catalog_source::aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    }
    delete_sources.assert_awaited_once_with("ws-catalog")


@pytest.mark.anyio
async def test_workspace_teardown_retains_catalog_rows_when_generated_purge_fails() -> None:
    delete_sources = AsyncMock()
    with (
        patch.object(mcp_lifecycle_store, "activate_workspace_fence", new=AsyncMock()),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_workspace",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.secret_store.purge_generated_catalog_secrets",
            new=AsyncMock(side_effect=RuntimeError("vault unavailable")),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.catalog_store.delete_workspace_sources",
            new=delete_sources,
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(
                "/api/v1/internal/mcp/workspaces/ws-catalog-failure",
                headers={"Authorization": "Bearer dev_orchestrator_token"},
            )

    assert response.status_code == 503
    delete_sources.assert_not_awaited()


@pytest.mark.anyio
async def test_nested_lifecycle_locks_share_one_advisory_connection(tmp_path) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    connection = MagicMock()
    connection.execute = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)
    connection.begin.return_value = transaction
    connect_context = MagicMock()
    connect_context.__aenter__ = AsyncMock(return_value=connection)
    connect_context.__aexit__ = AsyncMock(return_value=None)
    fake_engine = SimpleNamespace(
        connect=Mock(return_value=connect_context),
        dispose=AsyncMock(),
    )
    destination = McpDestination(
        workspace_id="ws-1",
        scope_type="target",
        destination_id="cluster-a",
        target_type="kubernetes",
    )
    store._advisory_lock_engine = fake_engine
    store._supports_advisory_locks = True
    try:
        async with (
            store.workspace_mutation_lock("ws-1"),
            store.destination_mutation_lock(destination),
            store.server_mutation_lock("ws-1", "server-1"),
        ):
            pass
        assert fake_engine.connect.call_count == 1
        assert connection.execute.await_count == 3
    finally:
        await store.close()


@pytest.mark.anyio
async def test_server_operation_keeps_cross_replica_session_lock_through_yield(
    tmp_path,
) -> None:
    stores = [await _sqlite_lifecycle_store(tmp_path) for _ in range(2)]
    advisory_locks: dict[int, asyncio.Lock] = {}

    class FakeConnection:
        def __init__(self) -> None:
            self.held: set[int] = set()

        async def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_advisory_unlock_all" in sql:
                for held_key in list(self.held):
                    self.held.remove(held_key)
                    advisory_locks[held_key].release()
                return Mock()
            key = int(params["lock_key"])
            lock = advisory_locks.setdefault(key, asyncio.Lock())
            if "pg_advisory_unlock" in sql:
                if key in self.held:
                    self.held.remove(key)
                    lock.release()
            elif "pg_advisory_lock" in sql:
                await lock.acquire()
                self.held.add(key)
            return Mock()

        async def close(self) -> None:
            for key in list(self.held):
                self.held.remove(key)
                advisory_locks[key].release()

        async def invalidate(self) -> None:
            await self.close()

    server = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        workspace_id="ws-1",
        scope_type="target",
        target_id="cluster-a",
        target_type="kubernetes",
        credential_epoch=1,
        credential_transitioning=False,
    )
    entered_first = asyncio.Event()
    entered_second = asyncio.Event()
    release_first = asyncio.Event()

    async def first_operation() -> None:
        async with stores[0].server_operation("ws-1", str(server.id)):
            entered_first.set()
            await release_first.wait()

    async def second_operation() -> None:
        alternate_spelling = str(server.id).replace("-", "")
        async with stores[1].server_operation("ws-1", alternate_spelling):
            entered_second.set()

    try:
        for store in stores:
            store._advisory_lock_engine = SimpleNamespace(
                connect=AsyncMock(side_effect=lambda: FakeConnection()),
                dispose=AsyncMock(),
            )
            store._supports_advisory_locks = True
            store._get_server = AsyncMock(return_value=server)
            store.assert_not_fenced = AsyncMock()

        first_task = asyncio.create_task(first_operation())
        await entered_first.wait()
        second_task = asyncio.create_task(second_operation())
        await asyncio.sleep(0.02)
        assert not entered_second.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert entered_second.is_set()
    finally:
        for store in stores:
            await store.close()


@pytest.mark.anyio
async def test_server_session_lock_cleanup_invalidates_on_unlock_failure(tmp_path) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    server = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        workspace_id="ws-1",
        scope_type="target",
        target_id="cluster-a",
        target_type="kubernetes",
        credential_epoch=1,
        credential_transitioning=False,
    )
    connection = SimpleNamespace()

    async def execute(statement, _params=None):
        if "pg_advisory_unlock_all" in str(statement):
            raise RuntimeError("unlock failed")
        return Mock()

    connection.execute = AsyncMock(side_effect=execute)
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    store._advisory_lock_engine = SimpleNamespace(
        connect=AsyncMock(return_value=connection),
        dispose=AsyncMock(),
    )
    store._supports_advisory_locks = True
    store._get_server = AsyncMock(return_value=server)
    store.assert_not_fenced = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match="unlock failed"):
            async with store.server_operation("ws-1", str(server.id)):
                pass
        connection.invalidate.assert_awaited_once()
        connection.close.assert_awaited_once()
        assert store._locks == {}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_server_session_lock_cleanup_is_shielded_from_caller_cancellation(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    server = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        workspace_id="ws-1",
        scope_type="target",
        target_id="cluster-a",
        target_type="kubernetes",
        credential_epoch=1,
        credential_transitioning=False,
    )
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    entered = asyncio.Event()
    connection = SimpleNamespace()

    async def execute(statement, _params=None):
        if "pg_advisory_unlock_all" in str(statement):
            cleanup_started.set()
            await allow_cleanup.wait()
        return Mock()

    connection.execute = AsyncMock(side_effect=execute)
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    store._advisory_lock_engine = SimpleNamespace(
        connect=AsyncMock(return_value=connection),
        dispose=AsyncMock(),
    )
    store._supports_advisory_locks = True
    store._get_server = AsyncMock(return_value=server)
    store.assert_not_fenced = AsyncMock()

    async def operate() -> None:
        async with store.server_operation("ws-1", str(server.id)):
            entered.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(operate())
        await entered.wait()
        task.cancel()
        await cleanup_started.wait()
        assert not task.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        connection.invalidate.assert_not_awaited()
        connection.close.assert_awaited_once()
        assert store._locks == {}
    finally:
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path,store_patch",
    [
        (
            "/api/v1/internal/mcp/servers?workspace_id=ws-1&target_id=cluster-a"
            "&target_type=kubernetes",
            "app.api.handlers_mcp_admin.mcp_server_registry.list_servers",
        ),
        (
            "/api/v1/internal/mcp/tools?workspace_id=ws-1&target_id=cluster-a"
            "&target_type=kubernetes",
            "app.api.handlers_mcp_admin.tool_registry.list_tools",
        ),
    ],
)
async def test_destination_list_probes_return_canonical_fence_error(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    store_patch: str,
) -> None:
    @asynccontextmanager
    async def fenced(_destination):
        raise McpLifecycleFencedError("fenced")
        yield

    monkeypatch.setattr(mcp_lifecycle_store, "destination_operation", fenced)
    with patch(store_patch, new=AsyncMock()) as registry_call:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                path,
                headers={"Authorization": "Bearer dev_orchestrator_token"},
            )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "MCP_LIFECYCLE_FENCED",
            "message": "MCP lifecycle teardown is in progress for this destination.",
            "retryable": False,
        }
    }
    registry_call.assert_not_awaited()


@pytest.mark.anyio
async def test_destination_teardown_deletes_builtin_and_is_idempotent() -> None:
    builtin = SimpleNamespace(
        id="srv-builtin",
        workspace_id="ws-1",
        scope_type="target",
        agent_id=None,
        target_id="cluster-a",
        target_type="kubernetes",
        provenance_type="builtin",
    )
    activate = AsyncMock()
    cleanup = AsyncMock()
    remove_tool = AsyncMock(return_value=True)
    delete_server = AsyncMock(return_value=True)
    with (
        patch.object(mcp_lifecycle_store, "activate_destination_fence", activate),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_servers",
            new=AsyncMock(side_effect=[[builtin], [], [], []]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.cleanup_server_connections",
            cleanup,
        ),
        patch(
            "app.api.handlers_mcp_lifecycle._resolve_tools_for_server",
            new=AsyncMock(return_value=[SimpleNamespace(tool_name="list_resources")]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.tool_registry.remove_tool",
            remove_tool,
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_server_registry.delete_server",
            delete_server,
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = [
                await client.delete(
                    "/api/v1/internal/mcp/destinations?workspace_id=ws-1"
                    "&scope_type=target&target_id=cluster-a&target_type=kubernetes",
                    headers={"Authorization": "Bearer dev_orchestrator_token"},
                )
                for _ in range(2)
            ]

    assert [response.status_code for response in responses] == [204, 204]
    assert activate.await_count == 2
    cleanup.assert_awaited_once_with("ws-1", "srv-builtin", reason="lifecycle_teardown")
    remove_tool.assert_awaited_once()
    delete_server.assert_awaited_once()


@pytest.mark.anyio
async def test_destination_teardown_releases_broad_locks_during_server_cleanup(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    server = SimpleNamespace(id="server-1")
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    fenced_destination = McpDestination(
        workspace_id="ws-1",
        scope_type="target",
        destination_id="cluster-a",
        target_type="kubernetes",
    )
    unrelated_destination = McpDestination(
        workspace_id="ws-1",
        scope_type="target",
        destination_id="cluster-b",
        target_type="kubernetes",
    )

    async def teardown_server(_server) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    try:
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_servers",
                new=AsyncMock(side_effect=[[server], []]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle._teardown_server",
                new=AsyncMock(side_effect=teardown_server),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                request = asyncio.create_task(
                    client.delete(
                        "/api/v1/internal/mcp/destinations?workspace_id=ws-1"
                        "&scope_type=target&target_id=cluster-a&target_type=kubernetes",
                        headers={"Authorization": "Bearer dev_orchestrator_token"},
                    )
                )
                await cleanup_started.wait()
                with pytest.raises(McpLifecycleFencedError):
                    async with asyncio.timeout(0.2):
                        async with store.destination_operation(fenced_destination):
                            pass
                async with asyncio.timeout(0.2):
                    async with store.destination_operation(unrelated_destination):
                        pass
                release_cleanup.set()
                response = await request

        assert response.status_code == 204
    finally:
        await store.close()


@pytest.mark.anyio
async def test_workspace_teardown_exposes_fence_without_waiting_for_server_cleanup(
    tmp_path,
) -> None:
    store = await _sqlite_lifecycle_store(tmp_path)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    destination = McpDestination(
        workspace_id="ws-1",
        scope_type="target",
        destination_id="cluster-a",
        target_type="kubernetes",
    )

    async def teardown_server(_server) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    try:
        with (
            patch("app.api.handlers_mcp_lifecycle.mcp_lifecycle_store", store),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
                new=AsyncMock(side_effect=[[SimpleNamespace(id="server-1")], []]),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle._teardown_server",
                new=AsyncMock(side_effect=teardown_server),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_workspace",
                new=AsyncMock(),
            ),
            patch(
                "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_workspace",
                new=AsyncMock(return_value=[]),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                request = asyncio.create_task(
                    client.delete(
                        "/api/v1/internal/mcp/workspaces/ws-1",
                        headers={"Authorization": "Bearer dev_orchestrator_token"},
                    )
                )
                await cleanup_started.wait()
                with pytest.raises(McpLifecycleFencedError):
                    async with asyncio.timeout(0.2):
                        async with store.destination_operation(destination):
                            pass
                release_cleanup.set()
                response = await request

        assert response.status_code == 204
    finally:
        await store.close()


@pytest.mark.anyio
async def test_workspace_teardown_removes_preconnection_oauth_flow_state() -> None:
    delete_flows = AsyncMock()
    with (
        patch.object(
            mcp_lifecycle_store,
            "activate_workspace_fence",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_server_registry.list_workspace_servers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.oauth_flow_store.delete_for_workspace",
            delete_flows,
        ),
        patch(
            "app.api.handlers_mcp_lifecycle.mcp_connection_store.list_for_workspace",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            mcp_lifecycle_store,
            "delete_user_lifecycles_for_workspace",
            new=AsyncMock(return_value=0),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(
                "/api/v1/internal/mcp/workspaces/ws-1",
                headers={"Authorization": "Bearer dev_orchestrator_token"},
            )

    assert response.status_code == 204
    delete_flows.assert_awaited_once_with("ws-1")


def _oauth_preparation(
    *, workspace_id: str, server_id: str, owner_id: str
) -> OAuthPreparationRecord:
    issuer = "https://auth.example.test"
    return OAuthPreparationRecord(
        workspace_id=workspace_id,
        server_id=server_id,
        owner_id=owner_id,
        membership_generation=1,
        browser_binding_hash="a" * 64,
        return_path=f"/workspaces/{workspace_id}",
        resource="https://mcp.example.test/mcp",
        candidates=[
            {
                "issuer": issuer,
                "issuer_origin": issuer,
                "registration_method": "cimd",
                "scopes": [],
                "offline_access_requested": False,
            }
        ],
        endpoint_snapshots={
            issuer: OAuthEndpointSnapshot(
                issuer=issuer,
                authorization_endpoint=f"{issuer}/authorize",
                token_endpoint=f"{issuer}/token",
            )
        },
        metadata_fingerprints={issuer: "f" * 64},
    )


@pytest.mark.anyio
async def test_oauth_flow_server_and_workspace_cleanup_remove_cross_index_state() -> None:
    store = OAuthFlowStore()
    store._redis = None
    server_handles = [
        await store.create_preparation(
            _oauth_preparation(
                workspace_id="ws-1",
                server_id="server-1",
                owner_id=owner_id,
            )
        )
        for owner_id in ("user-1", "user-2")
    ]
    remaining_handle = await store.create_preparation(
        _oauth_preparation(
            workspace_id="ws-2",
            server_id="server-2",
            owner_id="user-3",
        )
    )

    await store.delete_for_server("ws-1", "server-1")
    for handle in server_handles:
        with pytest.raises(McpOAuthError):
            await store.get_preparation(handle)
    assert (await store.get_preparation(remaining_handle)).workspace_id == "ws-2"
    indexed_records = set().union(*store._memory_indexes.values())
    assert indexed_records == set(store._memory)

    await store.delete_for_workspace("ws-2")
    assert store._memory == {}
    assert store._memory_indexes == {}


@pytest.mark.anyio
async def test_oauth_flow_user_cleanup_removes_every_cross_index_membership() -> None:
    store = OAuthFlowStore()
    store._redis = None
    removed_handles = [
        await store.create_preparation(
            _oauth_preparation(
                workspace_id="ws-1",
                server_id=server_id,
                owner_id="user-1",
            )
        )
        for server_id in ("server-1", "server-2")
    ]
    retained_handle = await store.create_preparation(
        _oauth_preparation(
            workspace_id="ws-1",
            server_id="server-1",
            owner_id="user-2",
        )
    )

    await store.delete_for_user("ws-1", "user-1")

    for handle in removed_handles:
        with pytest.raises(McpOAuthError):
            await store.get_preparation(handle)
    assert (await store.get_preparation(retained_handle)).owner_id == "user-2"
    indexed_records = set().union(*store._memory_indexes.values())
    assert indexed_records == set(store._memory)


@pytest.mark.anyio
async def test_offline_reset_purges_all_oauth_flow_records_and_indexes() -> None:
    store = OAuthFlowStore()
    store._redis = None
    for workspace_id, owner_id in (("ws-1", "former-1"), ("ws-2", "former-2")):
        await store.create_preparation(
            _oauth_preparation(
                workspace_id=workspace_id,
                server_id="server-1",
                owner_id=owner_id,
            )
        )

    state_object_count = await store.count_all_flow_records()
    assert state_object_count > 2
    assert await store.purge_all_flow_state() == state_object_count
    assert await store.count_all_flow_records() == 0
    assert store._memory == {}
    assert store._memory_indexes == {}


@pytest.mark.anyio
async def test_offline_reset_counts_and_purges_orphan_oauth_index_state() -> None:
    store = OAuthFlowStore()
    store._redis = None
    store._memory_indexes["gateway:mcp:oauth:index:orphan"] = set()

    assert await store.count_all_flow_records() == 1
    assert await store.purge_all_flow_state() == 1
    assert await store.count_all_flow_records() == 0


@pytest.mark.anyio
async def test_discovery_create_kill_switch_precedes_validation_and_all_side_effects() -> None:
    validate_contract = Mock()
    validate_egress = AsyncMock()
    create_server = AsyncMock()
    discover = AsyncMock()
    with (
        patch.object(settings, "REMOTE_MCP_ENABLED", False),
        patch(
            "app.api.handlers_mcp_admin.validate_remote_mcp_endpoint_contract",
            validate_contract,
        ),
        patch("app.api.handlers_mcp_admin.validate_mcp_server_url", validate_egress),
        patch(
            "app.api.handlers_mcp_admin.mcp_server_registry.create_server",
            create_server,
        ),
        patch("app.api.handlers_mcp_admin._discover_server_tools", discover),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/internal/mcp/servers",
                headers={"Authorization": "Bearer dev_orchestrator_token"},
                json={
                    "workspace_id": "ws-1",
                    "target_id": "cluster-a",
                    "target_type": "kubernetes",
                    "server_name": "disabled",
                    "server_url": "https://mcp.example.test/mcp",
                },
            )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MCP_REMOTE_DISABLED"
    validate_contract.assert_not_called()
    validate_egress.assert_not_awaited()
    create_server.assert_not_awaited()
    discover.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "route,payload,first_side_effect",
    [
        (
            "/api/v1/internal/mcp/servers/server-1/connections/user-1/oauth/prepare",
            {
                    "workspace_id": "ws-1",
                    "owner_id": "user-1",
                    "membership_generation": 1,
                    "browser_binding_hash": "a" * 64,
                "return_path": "/workspaces/ws-1",
            },
            "app.api.handlers_mcp_oauth._oauth_server",
        ),
        (
            "/api/v1/internal/mcp/servers/server-1/connections/user-1/oauth/start",
            {
                    "workspace_id": "ws-1",
                    "owner_id": "user-1",
                    "membership_generation": 1,
                    "browser_binding_hash": "a" * 64,
                "preparation_handle": "p" * 32,
                "consent_granted": True,
            },
            "app.api.handlers_mcp_oauth.oauth_flow_store.get_preparation",
        ),
        (
            "/api/v1/internal/mcp/oauth/complete",
            {
                "code": "authorization-code",
                    "state": "s" * 32,
                    "owner_id": "user-1",
                    "membership_generation": 1,
                    "browser_binding_hash": "a" * 64,
            },
            "app.api.handlers_mcp_oauth.oauth_flow_store.get_flow",
        ),
    ],
)
@pytest.mark.parametrize(
    "remote_enabled,oauth_enabled,expected_code",
    [
        (True, False, "MCP_OAUTH_DISABLED"),
        (False, True, "MCP_REMOTE_DISABLED"),
    ],
)
async def test_oauth_entrypoints_apply_both_kill_switches_before_side_effects(
    route: str,
    payload: dict[str, object],
    first_side_effect: str,
    remote_enabled: bool,
    oauth_enabled: bool,
    expected_code: str,
) -> None:
    side_effect = AsyncMock()
    with (
        patch.object(settings, "REMOTE_MCP_ENABLED", remote_enabled),
        patch.object(settings, "MCP_OAUTH_ENABLED", oauth_enabled),
        patch(first_side_effect, side_effect),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                route,
                headers={"Authorization": "Bearer dev_orchestrator_token"},
                json=payload,
            )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == expected_code
    side_effect.assert_not_awaited()
