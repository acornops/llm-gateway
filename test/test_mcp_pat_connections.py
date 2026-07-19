import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.handlers_mcp_connections import (
    _connection_response,
    _merge_personal_discovery,
    _mutation_lock,
    _personal_headers,
    _verify_connection,
    check_mcp_connection_readiness,
    delete_mcp_user_connection,
    put_mcp_user_connection,
    verify_mcp_user_connection,
)
from app.api.mcp_admin_schemas import (
    McpExactToolReference,
    McpPrincipalReference,
    McpReadinessRequest,
    McpUserConnectionUpsertRequest,
    McpUserConnectionVerifyRequest,
)
from app.api.mcp_runtime_auth import (
    mark_personal_connection_error,
    personal_connection_headers,
)
from app.config.settings import settings
from app.main import app


def _server(**overrides):
    values = {
        "id": "11111111-1111-4111-8111-111111111111",
        "workspace_id": "ws-1",
        "target_id": "target-1",
        "target_type": "kubernetes",
        "server_url": "https://mcp.example.com/mcp",
        "auth_scope": "personal",
        "auth_type": "bearer_token",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer ",
        "public_headers": {"x-client-version": "v1"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _claims(*, principal_type="user"):
    return SimpleNamespace(
        workspace_id="ws-1",
        scope=SimpleNamespace(type="workspace"),
        principal=SimpleNamespace(type=principal_type, id="user-1"),
    )


def test_connection_response_is_write_only_and_installation_derived() -> None:
    response = _connection_response(
        _server(auth_type="custom_header"),
        SimpleNamespace(status="error"),
    )

    assert response.model_dump() == {
        "server_id": "11111111-1111-4111-8111-111111111111",
        "status": "error",
        "auth_type": "custom_header",
        "action": "verify_mcp_server",
    }


def test_personal_headers_format_bearer_and_custom_header_pats() -> None:
    assert _personal_headers(_server(), "ws-1", "pat-value")["Authorization"] == (
        "Bearer pat-value"
    )
    custom = _server(
        auth_type="custom_header",
        auth_header_name="X-Api-Key",
        auth_header_prefix="Token ",
    )
    assert _personal_headers(custom, "ws-1", "pat-value")["X-Api-Key"] == (
        "Token pat-value"
    )


@pytest.mark.anyio
async def test_connection_mutations_are_serialized_per_installation_and_user() -> None:
    active = 0
    max_active = 0

    async def mutate() -> None:
        nonlocal active, max_active
        async with _mutation_lock("ws-1", str(_server().id), "user-1"):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    with (
        patch.object(settings, "APP_ENV", "development"),
        patch.object(settings, "NODE_ENV", None),
    ):
        await asyncio.gather(mutate(), mutate(), mutate())

    assert max_active == 1


@pytest.mark.anyio
async def test_production_mutations_use_the_cross_replica_database_lock() -> None:
    acquired: list[tuple[str, str, str]] = []

    @asynccontextmanager
    async def distributed_lock(workspace_id: str, server_id: str, user_id: str):
        acquired.append((workspace_id, server_id, user_id))
        yield

    with (
        patch.object(settings, "APP_ENV", "production"),
        patch.object(settings, "NODE_ENV", None),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.mutation_lock",
            side_effect=distributed_lock,
        ),
    ):
        async with _mutation_lock("ws-1", str(_server().id), "user-1"):
            pass

    assert acquired == [("ws-1", str(_server().id), "user-1")]


@pytest.mark.anyio
async def test_removed_oauth_and_service_connection_routes_return_404() -> None:
    server_id = "11111111-1111-4111-8111-111111111111"
    base = f"/api/v1/internal/mcp/servers/{server_id}/connections/user-1"
    removed_paths = [
        f"{base}/oauth/start",
        f"{base}/oauth/complete",
        f"{base}/oauth/client-credentials",
        f"{base}/service-connection",
    ]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in removed_paths:
            response = await client.post(path)
            assert response.status_code == 404, path


@pytest.mark.anyio
async def test_failed_verification_retains_connection_and_marks_error() -> None:
    connection = SimpleNamespace(status="error")
    with (
        patch(
            "app.api.handlers_mcp_connections._discover_server_tools",
            new=AsyncMock(return_value=([], "credential rejected")),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ) as set_state,
    ):
        result = await _verify_connection(
            server=_server(),
            connection=connection,
            workspace_id="ws-1",
            user_id="user-1",
            credential="bad-pat",
        )

    assert result is connection
    set_state.assert_awaited_once_with(
        connection, "error", error_code="MCP_PAT_VERIFICATION_FAILED"
    )


@pytest.mark.anyio
async def test_empty_discovery_is_a_connected_snapshot() -> None:
    connection = SimpleNamespace(status="error")
    connected = SimpleNamespace(status="connected", verified_tool_names=[])
    with (
        patch(
            "app.api.handlers_mcp_connections._discover_server_tools",
            new=AsyncMock(return_value=([], None)),
        ),
        patch(
            "app.api.handlers_mcp_connections._merge_personal_discovery",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connected),
        ) as set_state,
    ):
        result = await _verify_connection(
            server=_server(), connection=connection, workspace_id="ws-1",
            user_id="user-1", credential="valid-pat"
        )

    assert result is connected
    set_state.assert_awaited_once_with(
        connection, "connected", verified_tool_names=[]
    )


@pytest.mark.anyio
async def test_verify_retries_stored_pat_without_reentry() -> None:
    server = _server()
    connection = SimpleNamespace(status="error", access_secret_name="mcp_pat::server::user")
    verified = SimpleNamespace(status="connected", access_secret_name=connection.access_secret_name)
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections._check_mutation_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.get_secret",
            new=AsyncMock(return_value="stored-pat"),
        ),
        patch(
            "app.api.handlers_mcp_connections._verify_connection",
            new=AsyncMock(return_value=verified),
        ) as verify,
    ):
        response = await verify_mcp_user_connection(
            McpUserConnectionVerifyRequest(workspace_id="ws-1", user_id="user-1"),
            server_id=str(server.id),
            user_id="user-1",
        )

    verify.assert_awaited_once_with(
        server=server,
        connection=connection,
        workspace_id="ws-1",
        user_id="user-1",
        credential="stored-pat",
    )
    assert response.status == "connected"
    assert response.action is None


@pytest.mark.anyio
async def test_connect_retains_write_only_pat_when_verification_fails() -> None:
    server = _server()
    connection = SimpleNamespace(status="error", access_secret_name="mcp_pat::server::user")
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections._check_mutation_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.put_secret",
            new=AsyncMock(),
        ) as put_secret,
        patch(
            "app.api.handlers_mcp_connections.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_secret,
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.upsert",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.handlers_mcp_connections._verify_connection",
            new=AsyncMock(return_value=connection),
        ),
    ):
        response = await put_mcp_user_connection(
            McpUserConnectionUpsertRequest(
                workspace_id="ws-1",
                user_id="user-1",
                credential="write-only-pat",
                consent_granted=True,
            ),
            server_id=str(server.id),
            user_id="user-1",
        )

    put_secret.assert_awaited_once_with(
        f"mcp_pat::{server.id}::user-1", "write-only-pat", {"workspace_id": "ws-1"}
    )
    delete_secret.assert_not_awaited()
    assert "credential" not in response.model_dump()
    assert response.status == "error"


@pytest.mark.anyio
async def test_connect_compensates_new_secret_when_connection_persistence_fails() -> None:
    server = _server()
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections._check_mutation_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.put_secret",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_secret,
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.upsert",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),pytest.raises(RuntimeError, match="database unavailable")
    ):
        await put_mcp_user_connection(
            McpUserConnectionUpsertRequest(
                workspace_id="ws-1",
                user_id="user-1",
                credential="new-pat",
                consent_granted=True,
            ),
            server_id=str(server.id),
            user_id="user-1",
        )

    delete_secret.assert_awaited_once_with(
        f"mcp_pat::{server.id}::user-1", {"workspace_id": "ws-1"}
    )


@pytest.mark.anyio
async def test_rotation_restores_old_secret_when_connection_persistence_fails() -> None:
    server = _server()
    connection = SimpleNamespace(access_secret_name="mcp_pat::server::user")
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections._check_mutation_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.get_secret",
            new=AsyncMock(return_value="old-pat"),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.put_secret",
            new=AsyncMock(),
        ) as put_secret,
        patch(
            "app.api.handlers_mcp_connections.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_secret,
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.upsert",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),pytest.raises(RuntimeError, match="database unavailable")
    ):
        await put_mcp_user_connection(
            McpUserConnectionUpsertRequest(
                workspace_id="ws-1",
                user_id="user-1",
                credential="rotated-pat",
                consent_granted=True,
            ),
            server_id=str(server.id),
            user_id="user-1",
        )

    assert [item.args[1] for item in put_secret.await_args_list] == [
        "rotated-pat",
        "old-pat",
    ]
    delete_secret.assert_not_awaited()


@pytest.mark.anyio
async def test_disconnect_deletes_pat_and_connection_record() -> None:
    server = _server()
    connection = SimpleNamespace(
        access_secret_name="mcp_pat::server::user",
        workspace_id="ws-1",
        server_id=server.id,
        user_id="user-1",
    )
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_secret,
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.delete",
            new=AsyncMock(return_value=True),
        ) as delete_connection,
    ):
        await delete_mcp_user_connection(
            server_id=str(server.id), user_id="user-1", workspace_id="ws-1"
        )

    delete_secret.assert_awaited_once_with(
        connection.access_secret_name, {"workspace_id": "ws-1"}
    )
    delete_connection.assert_awaited_once_with("ws-1", str(server.id), "user-1")


@pytest.mark.anyio
async def test_disconnect_preserves_connection_row_when_secret_cleanup_fails() -> None:
    server = _server()
    connection = SimpleNamespace(
        access_secret_name="mcp_pat::server::user",
        workspace_id="ws-1",
        server_id=server.id,
        user_id="user-1",
    )
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.delete_secret",
            new=AsyncMock(side_effect=RuntimeError("secret backend unavailable")),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.delete",
            new=AsyncMock(),
        ) as delete_connection,pytest.raises(RuntimeError, match="secret backend unavailable")
    ):
        await delete_mcp_user_connection(
            server_id=str(server.id), user_id="user-1", workspace_id="ws-1"
        )

    delete_connection.assert_not_awaited()


@pytest.mark.anyio
async def test_disconnect_is_idempotent_when_connection_is_missing() -> None:
    server = _server()
    with (
        patch(
            "app.api.handlers_mcp_connections._get_personal_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_secret,
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.delete",
            new=AsyncMock(),
        ) as delete_connection,
    ):
        await delete_mcp_user_connection(
            server_id=str(server.id), user_id="user-1", workspace_id="ws-1"
        )

    delete_secret.assert_not_awaited()
    delete_connection.assert_not_awaited()


@pytest.mark.anyio
async def test_runtime_rejects_service_principal_for_personal_mcp() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await personal_connection_headers(
            _server(), _claims(principal_type="service_identity"), "records.list"
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "MCP_PAT_USER_PRINCIPAL_REQUIRED"


@pytest.mark.anyio
async def test_runtime_fails_closed_for_erroneous_connection() -> None:
    with (
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=SimpleNamespace(status="error")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await personal_connection_headers(_server(), _claims(), "records.list")

    assert exc_info.value.detail["action"] == "verify_mcp_server"


@pytest.mark.anyio
async def test_workflow_user_principal_reuses_selected_agent_connection() -> None:
    server = _server(target_id="agent-1", target_type="agent")
    connection = SimpleNamespace(
        status="connected", access_secret_name="mcp_pat::agent-server::user-1",
        verified_tool_names=["records.list"],
    )
    with (
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ) as get_connection,
        patch(
            "app.api.mcp_runtime_auth.secret_store.get_secret",
            new=AsyncMock(return_value="agent-pat"),
        ) as get_secret,
    ):
        headers = await personal_connection_headers(server, _claims(), "records.list")

    assert headers == {"Authorization": "Bearer agent-pat"}
    get_connection.assert_awaited_once_with("ws-1", str(server.id), "user-1")
    get_secret.assert_awaited_once_with(
        connection.access_secret_name, {"workspace_id": "ws-1"}
    )


@pytest.mark.anyio
async def test_runtime_auth_rejection_marks_only_the_current_installation_error() -> None:
    connection = SimpleNamespace(status="connected")
    with (
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ) as get_connection,
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ) as set_state,
    ):
        await mark_personal_connection_error(_server(), _claims())

    get_connection.assert_awaited_once_with(
        "ws-1", "11111111-1111-4111-8111-111111111111", "user-1"
    )
    set_state.assert_awaited_once_with(
        connection, "error", error_code="MCP_PAT_RUNTIME_AUTH_REJECTED"
    )


def test_pat_input_enforces_utf8_size_and_control_characters_without_normalizing() -> None:
    value = "  exact PAT value  "
    request = McpUserConnectionUpsertRequest(
        workspace_id="ws-1", user_id="user-1", credential=value,
        consent_granted=True,
    )
    assert request.credential == value

    with pytest.raises(ValueError):
        McpUserConnectionUpsertRequest(
            workspace_id="ws-1", user_id="user-1", credential="a\nvalue",
            consent_granted=True,
        )
    with pytest.raises(ValueError):
        McpUserConnectionUpsertRequest(
            workspace_id="ws-1", user_id="user-1", credential="é" * 4097,
            consent_granted=True,
        )


@pytest.mark.anyio
async def test_personal_discovery_only_adds_new_tools_as_pending() -> None:
    existing = SimpleNamespace(tool_name="records.list")
    discovered = [
        SimpleNamespace(name="records.list"),
        SimpleNamespace(name="records.write"),
    ]
    with (
        patch(
            "app.api.handlers_mcp_connections._resolve_tools_for_server",
            new=AsyncMock(return_value=[existing]),
        ),
        patch(
            "app.api.handlers_mcp_connections._apply_tools_for_server",
            new=AsyncMock(),
        ) as apply_tools,
    ):
        await _merge_personal_discovery(_server(), discovered)

    applied = apply_tools.await_args.args[3]
    assert [tool.name for tool in applied] == ["records.write"]
    assert apply_tools.await_args.kwargs["remove_disabled"] is False


@pytest.mark.anyio
async def test_readiness_is_tool_granular_for_each_user_snapshot() -> None:
    server = _server(enabled=True)
    approved_tool = SimpleNamespace(
        enabled=True, review_state="approved", source="mcp"
    )
    connection = SimpleNamespace(
        status="connected", verified_tool_names=["records.list"]
    )
    with (
        patch(
            "app.api.handlers_mcp_connections.mcp_server_registry.get_server_for_workspace",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.tool_registry.get_tool",
            new=AsyncMock(return_value=approved_tool),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
    ):
        response = await check_mcp_connection_readiness(
            McpReadinessRequest(
                workspace_id="ws-1",
                principal=McpPrincipalReference(type="user", id="user-1"),
                tool_refs=[
                    McpExactToolReference(server_id=str(server.id), tool_name="records.list"),
                    McpExactToolReference(server_id=str(server.id), tool_name="records.write"),
                ],
            )
        )

    assert response.ready is False
    assert [(failure.tool_name, failure.code) for failure in response.failures] == [
        ("records.write", "MCP_PERSONAL_TOOL_UNAVAILABLE")
    ]


@pytest.mark.anyio
async def test_readiness_accepts_enabled_pending_tools_from_trusted_builtin_server() -> None:
    server = _server(
        enabled=True,
        auth_scope="none",
        provenance_type="builtin",
    )
    builtin_tool = SimpleNamespace(
        enabled=True,
        review_state="pending",
        source="builtin",
    )
    with (
        patch(
            "app.api.handlers_mcp_connections.mcp_server_registry.get_server_for_workspace",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.tool_registry.get_tool",
            new=AsyncMock(return_value=builtin_tool),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(),
        ) as get_connection,
    ):
        response = await check_mcp_connection_readiness(
            McpReadinessRequest(
                workspace_id="ws-1",
                principal=McpPrincipalReference(type="user", id="user-1"),
                tool_refs=[
                    McpExactToolReference(
                        server_id=str(server.id),
                        tool_name="get_resource_logs",
                    )
                ],
            )
        )

    assert response.ready is True
    assert response.failures == []
    get_connection.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("server_provenance", "tool_source"),
    [("manual", "builtin"), ("builtin", "mcp")],
)
async def test_readiness_does_not_extend_builtin_trust_to_mismatched_registry_rows(
    server_provenance: str,
    tool_source: str,
) -> None:
    server = _server(
        enabled=True,
        auth_scope="none",
        provenance_type=server_provenance,
    )
    pending_tool = SimpleNamespace(
        enabled=True,
        review_state="pending",
        source=tool_source,
    )
    with (
        patch(
            "app.api.handlers_mcp_connections.mcp_server_registry.get_server_for_workspace",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.tool_registry.get_tool",
            new=AsyncMock(return_value=pending_tool),
        ),
    ):
        response = await check_mcp_connection_readiness(
            McpReadinessRequest(
                workspace_id="ws-1",
                principal=McpPrincipalReference(type="user", id="user-1"),
                tool_refs=[
                    McpExactToolReference(
                        server_id=str(server.id),
                        tool_name="get_resource_logs",
                    )
                ],
            )
        )

    assert response.ready is False
    assert [(failure.tool_name, failure.code) for failure in response.failures] == [
        ("get_resource_logs", "MCP_INSTALLATION_UNAVAILABLE")
    ]
