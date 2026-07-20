import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.handlers_mcp_connections import (
    _connection_response,
    _merge_connection_discovery,
    _mutation_lock,
    _verify_connection,
    check_mcp_connection_readiness,
)
from app.api.mcp_admin_schemas import (
    McpConnectionUpsertRequest,
    McpExactToolReference,
    McpPrincipalReference,
    McpReadinessRequest,
)
from app.api.mcp_runtime_auth import connection_request_headers
from app.config.settings import settings
from app.mcp.connections import (
    INSTALLATION_OWNER_ID,
    ConnectionOwner,
    ConnectionOwnerError,
    credential_secret_name,
    resolve_connection_owner,
)
from app.mcp.header_policy import build_mcp_request_headers


def _server(**overrides):
    values = {
        "id": "11111111-1111-4111-8111-111111111111",
        "workspace_id": "ws-1",
        "target_id": "target-1",
        "target_type": "kubernetes",
        "server_url": "https://mcp.example.com/mcp",
        "credential_mode": "individual",
        "auth_type": "bearer_token",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer ",
        "public_headers": {"x-client-version": "v1"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _claims(*, principal_type="user", principal_id="user-1"):
    return SimpleNamespace(
        workspace_id="ws-1",
        scope=SimpleNamespace(type="workspace"),
        principal=SimpleNamespace(type=principal_type, id=principal_id),
    )


def test_owner_resolution_is_mode_exact_and_has_no_fallback() -> None:
    assert resolve_connection_owner(_server(credential_mode="none"), "user", "u-1") is None
    assert resolve_connection_owner(
        _server(credential_mode="workspace"), "service_identity", "svc-1"
    ) == ConnectionOwner("installation", INSTALLATION_OWNER_ID)
    assert resolve_connection_owner(
        _server(credential_mode="individual"), "user", "u-1"
    ) == ConnectionOwner("user", "u-1")
    with pytest.raises(ConnectionOwnerError):
        resolve_connection_owner(_server(credential_mode="individual"), "service_identity", "svc-1")


def test_secret_identity_is_deterministic_and_owner_scoped() -> None:
    server_id = str(_server().id)
    assert (
        credential_secret_name(
            "ws-1", server_id, ConnectionOwner("installation", INSTALLATION_OWNER_ID)
        )
        == f"mcp_credential::ws-1::{server_id}::installation"
    )
    assert (
        credential_secret_name("ws-1", server_id, ConnectionOwner("user", "user-1"))
        == f"mcp_credential::ws-1::{server_id}::user::user-1"
    )


def test_common_header_builder_formats_bearer_and_custom_credentials() -> None:
    headers = build_mcp_request_headers(
        _server(), "token", platform_headers={"x-workspace-id": "ws-1"}
    )
    assert headers == {
        "x-client-version": "v1",
        "x-workspace-id": "ws-1",
        "Authorization": "Bearer token",
    }
    custom = _server(
        auth_type="custom_header",
        auth_header_name="X-Api-Key",
        auth_header_prefix="Token ",
    )
    assert build_mcp_request_headers(custom, "token")["X-Api-Key"] == "Token token"
    with pytest.raises(ValueError):
        build_mcp_request_headers(custom, "bad\nvalue")


def test_connection_response_is_secret_free_and_mode_derived() -> None:
    response = _connection_response(
        _server(credential_mode="workspace", auth_type="custom_header"),
        SimpleNamespace(status="error", error_code="MCP_CREDENTIAL_VERIFICATION_FAILED"),
    )
    assert response.model_dump() == {
        "server_id": "11111111-1111-4111-8111-111111111111",
        "credential_mode": "workspace",
        "status": "error",
        "auth_type": "custom_header",
        "action": "verify_mcp_server",
        "error_code": "MCP_CREDENTIAL_VERIFICATION_FAILED",
        "verified_at": None,
        "updated_at": None,
    }


def test_credential_input_preserves_value_and_rejects_control_characters() -> None:
    request = McpConnectionUpsertRequest(
        workspace_id="ws-1",
        owner_type="user",
        owner_id="user-1",
        credential="  exact value  ",
        consent_granted=True,
    )
    assert request.credential == "  exact value  "
    with pytest.raises(ValueError):
        McpConnectionUpsertRequest(
            workspace_id="ws-1",
            owner_type="user",
            owner_id="user-1",
            credential="bad\nvalue",
            consent_granted=True,
        )


@pytest.mark.anyio
async def test_connection_mutations_are_serialized_per_owner() -> None:
    active = 0
    max_active = 0
    owner = ConnectionOwner("user", "user-1")

    async def mutate() -> None:
        nonlocal active, max_active
        async with _mutation_lock("ws-1", str(_server().id), owner):
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
async def test_production_mutations_use_cross_replica_owner_lock() -> None:
    acquired: list[tuple[str, str, ConnectionOwner]] = []
    owner = ConnectionOwner("installation", INSTALLATION_OWNER_ID)

    @asynccontextmanager
    async def distributed_lock(workspace_id: str, server_id: str, lock_owner):
        acquired.append((workspace_id, server_id, lock_owner))
        yield

    with (
        patch.object(settings, "APP_ENV", "production"),
        patch.object(settings, "NODE_ENV", None),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.mutation_lock",
            side_effect=distributed_lock,
        ),
    ):
        async with _mutation_lock("ws-1", str(_server().id), owner):
            pass
    assert acquired == [("ws-1", str(_server().id), owner)]


@pytest.mark.anyio
async def test_failed_verification_retains_bounded_error_state() -> None:
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
            credential="bad-credential",
        )
    assert result is connection
    set_state.assert_awaited_once_with(
        connection, "error", error_code="MCP_CREDENTIAL_VERIFICATION_FAILED"
    )


@pytest.mark.anyio
async def test_discovery_adds_only_new_tools_to_installation_catalog() -> None:
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
        await _merge_connection_discovery(_server(), discovered)
    assert [tool.name for tool in apply_tools.await_args.args[3]] == ["records.write"]
    assert apply_tools.await_args.kwargs["remove_disabled"] is False


@pytest.mark.anyio
async def test_workspace_mode_allows_service_identity_runtime() -> None:
    server = _server(credential_mode="workspace")
    owner = ConnectionOwner("installation", INSTALLATION_OWNER_ID)
    connection = SimpleNamespace(status="connected", verified_tool_names=["records.list"])
    with (
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ) as get_connection,
        patch(
            "app.api.mcp_runtime_auth.secret_store.get_secret",
            new=AsyncMock(return_value="workspace-token"),
        ),
    ):
        headers = await connection_request_headers(
            server,
            _claims(principal_type="service_identity", principal_id="svc-1"),
            "records.list",
            platform_headers={"x-workspace-id": "ws-1"},
        )
    assert headers["Authorization"] == "Bearer workspace-token"
    get_connection.assert_awaited_once_with("ws-1", str(server.id), owner)


@pytest.mark.anyio
async def test_individual_mode_rejects_service_identity_runtime() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await connection_request_headers(
            _server(),
            _claims(principal_type="service_identity", principal_id="svc-1"),
            "records.list",
            platform_headers={},
        )
    assert exc_info.value.detail["code"] == "MCP_INDIVIDUAL_USER_PRINCIPAL_REQUIRED"


@pytest.mark.anyio
async def test_runtime_fails_closed_during_credential_transition() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await connection_request_headers(
            _server(credential_transitioning=True),
            _claims(),
            "records.list",
            platform_headers={},
        )
    assert exc_info.value.detail["code"] == "MCP_INSTALLATION_UNAVAILABLE"


@pytest.mark.anyio
async def test_readiness_is_tool_granular_for_resolved_owner_snapshot() -> None:
    server = _server(enabled=True)
    approved_tool = SimpleNamespace(enabled=True, review_state="approved", source="mcp")
    connection = SimpleNamespace(status="connected", verified_tool_names=["records.list"])
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
    assert [(item.tool_name, item.code) for item in response.failures] == [
        ("records.write", "MCP_CREDENTIAL_TOOL_UNAVAILABLE")
    ]
