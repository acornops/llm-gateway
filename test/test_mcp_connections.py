import asyncio
import uuid
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.handlers_mcp_connections import (
    _verify_connection,
    check_mcp_connection_readiness,
    delete_mcp_connection,
    put_mcp_connection,
)
from app.api.mcp_admin_helpers import merge_connection_discovery
from app.api.mcp_admin_schemas import (
    McpConnectionUpsertRequest,
    McpConnectionVerifyRequest,
    McpExactToolReference,
    McpPrincipalReference,
    McpReadinessRequest,
)
from app.api.mcp_connection_cleanup import (
    cleanup_server_connections,
    cleanup_user_server_connection,
)
from app.api.mcp_connection_responses import connection_response
from app.api.mcp_runtime_auth import connection_request_headers, mark_connection_error
from app.config.settings import settings
from app.mcp.connections import (
    INSTALLATION_OWNER_ID,
    ConnectionOwner,
    ConnectionOwnerError,
    McpConnectionStore,
    credential_secret_name,
    mcp_connection_store,
    resolve_connection_owner,
)
from app.mcp.header_policy import build_mcp_request_headers
from app.mcp.lifecycle import McpUserLifecycleStaleError, mcp_lifecycle_store
from app.mcp.oauth.tokens import oauth_token_secret_name
from app.mcp.registry.models import McpServer
from app.mcp.tool_definition_policy import McpToolDefinitionConflictError
from app.secrets.db_models import Base


def _server(**overrides):
    values = {
        "id": "11111111-1111-4111-8111-111111111111",
        "workspace_id": "ws-1",
        "target_id": "target-1",
        "target_type": "kubernetes",
        "scope_type": "target",
        "agent_id": None,
        "server_url": "https://mcp.example.com/mcp",
        "credential_mode": "individual",
        "auth_type": "bearer_token",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer ",
        "public_headers": {"x-client-version": "v1"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _claims(
    *,
    principal_type="user",
    principal_id="user-1",
    membership_generation=None,
    scope_type="workspace",
):
    return SimpleNamespace(
        workspace_id="ws-1",
        scope=SimpleNamespace(type=scope_type),
        principal=SimpleNamespace(
            type=principal_type,
            id=principal_id,
            membership_generation=membership_generation,
        ),
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
    uuid_alias = server_id.replace("-", "")
    assert credential_secret_name(
        "ws-1", uuid_alias, ConnectionOwner("user", "user-1")
    ) == credential_secret_name(
        "ws-1", server_id, ConnectionOwner("user", "user-1")
    )
    assert oauth_token_secret_name(
        "ws-1", uuid_alias, "user-1"
    ) == oauth_token_secret_name("ws-1", server_id, "user-1")


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
    response = connection_response(
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
        "issuer_origin": None,
        "registration_method": None,
        "scopes": [],
        "token_expires_at": None,
        "refresh_capable": False,
        "verified_at": None,
        "updated_at": None,
    }


def test_credential_input_preserves_value_and_rejects_control_characters() -> None:
    request = McpConnectionUpsertRequest(
        workspace_id="ws-1",
        owner_type="user",
        owner_id="user-1",
        membership_generation=1,
        credential="  exact value  ",
        consent_granted=True,
    )
    assert request.credential == "  exact value  "
    with pytest.raises(ValueError):
        McpConnectionUpsertRequest(
            workspace_id="ws-1",
            owner_type="user",
            owner_id="user-1",
            membership_generation=1,
            credential="bad\nvalue",
            consent_granted=True,
        )


def test_owner_and_readiness_generation_contract_is_identity_specific() -> None:
    installation = McpConnectionUpsertRequest(
        workspace_id="ws-1",
        owner_type="installation",
        owner_id=INSTALLATION_OWNER_ID,
        credential="workspace-secret",
        consent_granted=True,
    )
    assert installation.membership_generation is None
    assert (
        McpConnectionVerifyRequest(
            workspace_id="ws-1",
            owner_type="installation",
            owner_id=INSTALLATION_OWNER_ID,
        ).membership_generation
        is None
    )
    assert (
        McpPrincipalReference(type="service_identity", id="service-1")
        .membership_generation
        is None
    )

    with pytest.raises(ValueError):
        McpConnectionUpsertRequest(
            workspace_id="ws-1",
            owner_type="user",
            owner_id="user-1",
            credential="user-secret",
            consent_granted=True,
        )
    with pytest.raises(ValueError):
        McpConnectionVerifyRequest(
            workspace_id="ws-1",
            owner_type="user",
            owner_id="user-1",
        )
    with pytest.raises(ValueError):
        McpPrincipalReference(type="user", id="user-1")
    with pytest.raises(ValueError):
        McpPrincipalReference(
            type="service_identity",
            id="service-1",
            membership_generation=1,
        )


@pytest.mark.anyio
async def test_replacing_credential_marks_connection_non_ready_before_secret_write() -> None:
    server = _server()
    request = McpConnectionUpsertRequest(
        workspace_id="ws-1",
        owner_type="user",
        owner_id="user-1",
        membership_generation=7,
        credential="new-credential",
        consent_granted=True,
    )
    existing = SimpleNamespace(
        status="connected",
        verified_tool_names=["records.list"],
        error_code=None,
    )
    pending = SimpleNamespace(
        status="error",
        error_code="MCP_CREDENTIAL_VERIFICATION_PENDING",
    )
    verified = SimpleNamespace(
        status="connected",
        error_code=None,
        verified_at=None,
        updated_at=None,
    )
    live = {"row_status": "connected", "secret": "old-credential"}

    async def persist_pending(**_kwargs):
        assert live == {"row_status": "connected", "secret": "old-credential"}
        live["row_status"] = "pending"
        return pending

    async def replace_secret(_name, value, _scope):
        # This is the crash-sensitive boundary: the persisted row must already
        # prevent runtime from reading the replacement credential.
        assert live["row_status"] == "pending"
        live["secret"] = value

    async def verify(**_kwargs):
        assert live == {"row_status": "pending", "secret": "new-credential"}
        live["row_status"] = "connected"
        return verified

    with (
        patch(
            "app.api.handlers_mcp_connections._get_connection_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections._assert_owner_generation",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections._check_mutation_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.upsert",
            new=AsyncMock(side_effect=persist_pending),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.get_secret",
            new=AsyncMock(return_value="old-credential"),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.put_secret",
            new=AsyncMock(side_effect=replace_secret),
        ),
        patch(
            "app.api.handlers_mcp_connections._verify_connection",
            new=AsyncMock(side_effect=verify),
        ),
    ):
        response = await put_mcp_connection(
            request=request,
            server_id=str(server.id),
            owner_id="user-1",
        )

    assert response.status == "connected"
    assert live == {"row_status": "connected", "secret": "new-credential"}


@pytest.mark.anyio
async def test_secret_write_failure_restores_prior_connected_state_and_secret() -> None:
    server = _server()
    request = McpConnectionUpsertRequest(
        workspace_id="ws-1",
        owner_type="user",
        owner_id="user-1",
        membership_generation=7,
        credential="new-credential",
        consent_granted=True,
    )
    existing = SimpleNamespace(
        status="connected",
        verified_tool_names=["records.list"],
        error_code=None,
    )
    pending = SimpleNamespace(status="error")
    live = {"row_status": "connected", "secret": "old-credential"}

    async def persist_pending(**_kwargs):
        live["row_status"] = "pending"
        return pending

    async def put_secret(_name, value, _scope):
        assert live["row_status"] == "pending"
        if value == "new-credential":
            raise RuntimeError("secret backend write failed")
        live["secret"] = value

    async def restore_state(_connection, status, **_kwargs):
        assert live["secret"] == "old-credential"
        live["row_status"] = status
        return existing

    with (
        patch(
            "app.api.handlers_mcp_connections._get_connection_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections._assert_owner_generation",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections._check_mutation_rate_limit",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.get",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.upsert",
            new=AsyncMock(side_effect=persist_pending),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.set_state",
            new=AsyncMock(side_effect=restore_state),
        ) as set_state,
        patch(
            "app.api.handlers_mcp_connections.secret_store.get_secret",
            new=AsyncMock(return_value="old-credential"),
        ),
        patch(
            "app.api.handlers_mcp_connections.secret_store.put_secret",
            new=AsyncMock(side_effect=put_secret),
        ) as put_secret_mock,pytest.raises(RuntimeError, match="secret backend write failed")
    ):
        await put_mcp_connection(
            request=request,
            server_id=str(server.id),
            owner_id="user-1",
        )

    assert [call.args[1] for call in put_secret_mock.await_args_list] == [
        "new-credential",
        "old-credential",
    ]
    set_state.assert_awaited_once()
    assert live == {"row_status": "connected", "secret": "old-credential"}


@pytest.mark.anyio
async def test_connection_mutations_are_serialized_per_owner() -> None:
    active = 0
    max_active = 0
    owner = ConnectionOwner("user", "user-1")
    canonical_server_id = str(_server().id)

    async def mutate(server_id: str) -> None:
        nonlocal active, max_active
        async with mcp_connection_store.mutation_lock(
            "ws-1",
            server_id,
            owner,
        ):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    with (
        patch.object(settings, "APP_ENV", "development"),
        patch.object(settings, "NODE_ENV", None),
    ):
        await asyncio.gather(
            mutate(canonical_server_id),
            mutate(canonical_server_id.replace("-", "")),
            mutate(f"{{{canonical_server_id}}}"),
        )
    assert max_active == 1


@pytest.mark.anyio
async def test_production_mutations_use_cross_replica_owner_lock() -> None:
    owner = ConnectionOwner("installation", INSTALLATION_OWNER_ID)
    connection = MagicMock()
    connection.execute = AsyncMock()
    transaction = MagicMock()
    connection.begin.return_value = transaction
    connect_context = MagicMock()
    connect_context.__aenter__ = AsyncMock(return_value=connection)
    connect_context.__aexit__ = AsyncMock(return_value=None)
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(settings, "APP_ENV", "production"),
        patch.object(settings, "NODE_ENV", None),
        patch.object(mcp_connection_store, "_supports_advisory_locks", True),
        patch.object(
            mcp_connection_store,
            "_advisory_lock_engine",
            SimpleNamespace(connect=MagicMock(return_value=connect_context)),
        ),
    ):
        async with mcp_connection_store.mutation_lock(
            "ws-1",
            str(_server().id),
            owner,
        ):
            pass
    assert "pg_advisory_xact_lock" in str(connection.execute.await_args.args[0])
    transaction.__aexit__.assert_awaited_once()


@pytest.mark.anyio
async def test_connection_store_persists_generation_only_for_individual_owner(
    tmp_path,
) -> None:
    store = McpConnectionStore(
        f"sqlite+aiosqlite:///{tmp_path / 'connection-generation.db'}"
    )
    user_server = McpServer(
        id=uuid.uuid4(),
        workspace_id="ws-1",
        scope_type="target",
        target_id="target-user",
        target_type="kubernetes",
        server_name="user-credentials",
        server_url="https://user-mcp.example.test/mcp",
        enabled=True,
        auth_type="bearer_token",
        credential_mode="individual",
        provenance_type="manual",
    )
    installation_server = McpServer(
        id=uuid.uuid4(),
        workspace_id="ws-1",
        scope_type="target",
        target_id="target-installation",
        target_type="kubernetes",
        server_name="workspace-credentials",
        server_url="https://workspace-mcp.example.test/mcp",
        enabled=True,
        auth_type="bearer_token",
        credential_mode="workspace",
        provenance_type="manual",
    )
    try:
        async with store.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with store.async_session() as session:
            session.add_all([user_server, installation_server])
            await session.commit()

        user_owner = ConnectionOwner("user", "user-1")
        installation_owner = ConnectionOwner("installation", INSTALLATION_OWNER_ID)
        user_connection = await store.upsert(
            workspace_id="ws-1",
            server_id=str(user_server.id),
            owner=user_owner,
            status="connected",
            membership_generation=7,
        )
        installation_connection = await store.upsert(
            workspace_id="ws-1",
            server_id=str(installation_server.id),
            owner=installation_owner,
            status="connected",
            membership_generation=7,
        )

        assert user_connection is not None
        assert user_connection.membership_generation == 7
        assert (
            await store.get("ws-1", str(user_server.id), user_owner)
        ).membership_generation == 7
        assert installation_connection is not None
        assert installation_connection.membership_generation is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_failed_verification_retains_bounded_error_state() -> None:
    connection = SimpleNamespace(status="error")
    with (
        patch(
            "app.api.handlers_mcp_connections._discover_server_tools",
            new=AsyncMock(
                return_value=(
                    [],
                    "MCP endpoint returned Not Found",
                    "MCP_ENDPOINT_NOT_FOUND",
                )
            ),
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
    set_state.assert_awaited_once_with(connection, "error", error_code="MCP_ENDPOINT_NOT_FOUND")


@pytest.mark.anyio
async def test_agent_verification_uses_only_agent_destination_context() -> None:
    connection = SimpleNamespace(status="error")
    discover = AsyncMock(return_value=([], "unavailable", "MCP_ENDPOINT_UNAVAILABLE"))
    agent_server = _server(scope_type="agent", agent_id="agent-1")
    del agent_server.target_id
    del agent_server.target_type
    with (
        patch(
            "app.api.handlers_mcp_connections._discover_server_tools",
            new=discover,
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ),
    ):
        await _verify_connection(
            server=agent_server,
            connection=connection,
            workspace_id="ws-1",
            credential="agent-credential",
        )

    assert discover.await_args.args[1] == "agent-1"
    headers = discover.await_args.kwargs["request_headers"]
    assert headers["x-agent-id"] == "agent-1"
    assert "x-target-id" not in headers
    assert "x-target-type" not in headers


@pytest.mark.anyio
async def test_oauth_authentication_rejection_requires_reauthorization() -> None:
    connection = SimpleNamespace(status="error")
    server = _server()
    server.auth_type = "oauth"
    with (
        patch(
            "app.api.handlers_mcp_connections._discover_server_tools",
            new=AsyncMock(
                return_value=(
                    [],
                    "MCP server rejected the configured credential",
                    "MCP_AUTHENTICATION_REJECTED",
                )
            ),
        ),
        patch(
            "app.api.handlers_mcp_connections.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ) as set_state,
    ):
        result = await _verify_connection(
            server=server,
            connection=connection,
            workspace_id="ws-1",
            credential="rejected-oauth-token",
        )

    assert result is connection
    set_state.assert_awaited_once_with(
        connection,
        "reauthorization_required",
        error_code="MCP_AUTHENTICATION_REJECTED",
    )


@pytest.mark.anyio
async def test_stale_oauth_failure_does_not_invalidate_a_reauthorized_connection() -> None:
    server = _server(auth_type="oauth")
    connection = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        oauth_scopes=["mcp:read"],
    )
    set_state = AsyncMock()
    with (
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.mcp_runtime_auth.oauth_token_service.access_token_matches",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.set_state",
            new=set_state,
        ),
    ):
        await mark_connection_error(
            server,
            _claims(),
            auth_error="invalid_token",
            expected_connection_id=str(connection.id),
            expected_credential_fingerprint="f" * 64,
        )

    set_state.assert_not_awaited()


@pytest.mark.anyio
async def test_discovery_adds_only_new_tools_to_installation_catalog() -> None:
    existing = SimpleNamespace(tool_name="records.list")
    discovered = [
        SimpleNamespace(name="records.list"),
        SimpleNamespace(name="records.write"),
    ]
    with (
        patch(
            "app.api.mcp_admin_helpers._resolve_tools_for_server",
            new=AsyncMock(return_value=[existing]),
        ),
        patch(
            "app.api.mcp_admin_helpers._apply_tools_for_server",
            new=AsyncMock(),
        ) as apply_tools,
    ):
        await merge_connection_discovery(_server(), discovered)
    assert [tool.name for tool in apply_tools.await_args.args[2]] == ["records.write"]
    assert apply_tools.await_args.kwargs["remove_disabled"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("existing", "observed"),
    [
        (
            SimpleNamespace(
                tool_name="records.list",
                input_schema={"type": "object"},
                output_schema=None,
                capability="write",
            ),
            SimpleNamespace(
                name="records.list",
                input_schema={"type": "string"},
                output_schema=None,
                capability="write",
            ),
        ),
    ],
)
async def test_user_discovery_rejects_security_relevant_shared_tool_conflicts(
    existing: SimpleNamespace,
    observed: SimpleNamespace,
) -> None:
    with (
        patch(
            "app.api.mcp_admin_helpers._resolve_tools_for_server",
            new=AsyncMock(return_value=[existing]),
        ),
        patch(
            "app.api.mcp_admin_helpers._apply_tools_for_server",
            new=AsyncMock(),
        ) as apply_tools,
        pytest.raises(McpToolDefinitionConflictError),
    ):
        await merge_connection_discovery(_server(), [observed])

    apply_tools.assert_not_awaited()


@pytest.mark.anyio
async def test_user_read_only_capability_override_allows_rediscovery() -> None:
    existing = SimpleNamespace(
        tool_name="records.list",
        input_schema={"type": "object"},
        output_schema=None,
        capability="read",
    )
    observed = SimpleNamespace(
        name="records.list",
        input_schema={"type": "object"},
        output_schema=None,
        capability="write",
    )
    with (
        patch(
            "app.api.mcp_admin_helpers._resolve_tools_for_server",
            new=AsyncMock(return_value=[existing]),
        ),
        patch(
            "app.api.mcp_admin_helpers._apply_tools_for_server",
            new=AsyncMock(),
        ) as apply_tools,
    ):
        verified_tool_names = await merge_connection_discovery(_server(), [observed])

    assert verified_tool_names == ["records.list"]
    apply_tools.assert_not_awaited()


@pytest.mark.anyio
async def test_workspace_mode_allows_service_identity_runtime() -> None:
    server = _server(credential_mode="workspace")
    owner = ConnectionOwner("installation", INSTALLATION_OWNER_ID)
    connection = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        status="connected",
        verified_tool_names=["records.list"],
    )
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
async def test_workspace_connection_ignores_user_generation_and_remains_unaffected() -> None:
    server = _server(credential_mode="workspace")
    connection = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        status="connected",
        verified_tool_names=["records.list"],
    )
    lifecycle_check = AsyncMock()
    with (
        patch.object(mcp_lifecycle_store, "assert_user_active", lifecycle_check),
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.mcp_runtime_auth.secret_store.get_secret",
            new=AsyncMock(return_value="workspace-token"),
        ),
    ):
        headers = await connection_request_headers(
            server,
            _claims(membership_generation=9),
            "records.list",
            platform_headers={},
        )
    assert headers["Authorization"] == "Bearer workspace-token"
    lifecycle_check.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("scope_type", ["target", "workspace"])
async def test_target_and_agent_runtime_reject_stale_connection_generation(
    scope_type: str,
) -> None:
    connection = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        status="connected",
        verified_tool_names=["records.list"],
        membership_generation=1,
    )
    secret_read = AsyncMock(return_value="old-token")
    with (
        patch.object(
            mcp_lifecycle_store,
            "assert_user_active",
            new=AsyncMock(return_value=SimpleNamespace(status="active")),
        ),
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.mcp_runtime_auth.secret_store.get_secret",
            new=secret_read,
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await connection_request_headers(
            _server(),
            _claims(membership_generation=2, scope_type=scope_type),
            "records.list",
            platform_headers={},
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "MCP_USER_LIFECYCLE_STALE"
    secret_read.assert_not_awaited()


@pytest.mark.anyio
async def test_post_response_error_write_refuses_stale_membership_generation() -> None:
    connection = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        membership_generation=1,
    )
    set_state = AsyncMock()
    with (
        patch.object(
            mcp_lifecycle_store,
            "assert_user_active",
            new=AsyncMock(return_value=SimpleNamespace(status="active")),
        ),
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.api.mcp_runtime_auth.mcp_connection_store.set_state",
            new=set_state,
        ),
    ):
        await mark_connection_error(
            _server(),
            _claims(membership_generation=2),
        )
    set_state.assert_not_awaited()


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
    connection = SimpleNamespace(
        status="connected",
        verified_tool_names=["records.list"],
        membership_generation=1,
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
                principal=McpPrincipalReference(
                    type="user", id="user-1", membership_generation=1
                ),
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


@pytest.mark.anyio
async def test_readiness_returns_top_level_stale_generation_error() -> None:
    server = _server(enabled=True)
    with (
        patch(
            "app.api.handlers_mcp_connections.mcp_server_registry.get_server_for_workspace",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_mcp_connections.tool_registry.get_tool",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enabled=True,
                    review_state="approved",
                    source="mcp",
                )
            ),
        ),
        patch.object(
            mcp_lifecycle_store,
            "assert_user_active",
            new=AsyncMock(side_effect=McpUserLifecycleStaleError("stale")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await check_mcp_connection_readiness(
            McpReadinessRequest(
                workspace_id="ws-1",
                principal=McpPrincipalReference(
                    type="user",
                    id="user-1",
                    membership_generation=2,
                ),
                tool_refs=[
                    McpExactToolReference(
                        server_id=str(server.id),
                        tool_name="records.list",
                    )
                ],
            )
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "MCP_USER_LIFECYCLE_STALE"


@pytest.mark.anyio
async def test_idempotent_disconnect_deletes_rowless_generic_oauth_and_flow_state() -> None:
    server = _server(auth_type="bearer_token")
    owner = ConnectionOwner("user", "user-1")

    @asynccontextmanager
    async def owner_lock(*_args):
        yield

    delete_generic = AsyncMock()
    delete_oauth = AsyncMock()
    delete_flow = AsyncMock()
    delete_row = AsyncMock()
    with (
        patch(
            "app.api.handlers_mcp_connections._get_connection_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.mutation_lock",
            new=owner_lock,
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.delete_secret",
            new=delete_generic,
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_token_service.delete_tokens",
            new=delete_oauth,
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_token_service.revoke",
            new=AsyncMock(),
        ) as revoke,
        patch(
            "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_connection",
            new=delete_flow,
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.delete",
            new=delete_row,
        ),
    ):
        await delete_mcp_connection(
            server_id=str(server.id),
            owner_id="user-1",
            workspace_id="ws-1",
            owner_type="user",
            membership_generation=1,
            _token_ok=None,
        )

    delete_flow.assert_awaited_once_with("ws-1", str(server.id), "user-1")
    delete_generic.assert_awaited_once_with(
        credential_secret_name("ws-1", str(server.id), owner),
        {"workspace_id": "ws-1"},
    )
    delete_oauth.assert_awaited_once_with("ws-1", str(server.id), "user-1")
    revoke.assert_not_awaited()
    delete_row.assert_not_awaited()


@pytest.mark.anyio
async def test_disconnect_retains_row_until_secret_cleanup_retry_succeeds() -> None:
    server = _server(auth_type="bearer_token")
    owner = ConnectionOwner("user", "user-1")
    connection = SimpleNamespace(
        workspace_id="ws-1",
        server_id=server.id,
        owner_type="user",
        owner_id="user-1",
        membership_generation=1,
        oauth_issuer=None,
    )

    @asynccontextmanager
    async def owner_lock(*_args):
        yield

    delete_row = AsyncMock(return_value=True)
    delete_oauth = AsyncMock(side_effect=[RuntimeError("secret unavailable"), None])
    def common_patches():
        return (
            patch(
                "app.api.handlers_mcp_connections._get_connection_server",
                new=AsyncMock(return_value=server),
            ),
            patch(
                "app.api.handlers_mcp_connections.mcp_connection_store.mutation_lock",
                new=owner_lock,
            ),
            patch(
                "app.api.handlers_mcp_connections.mcp_connection_store.get",
                new=AsyncMock(return_value=connection),
            ),
            patch(
                "app.api.mcp_connection_cleanup.secret_store.delete_secret",
                new=AsyncMock(),
            ),
            patch(
                "app.api.mcp_connection_cleanup.oauth_token_service.delete_tokens",
                new=delete_oauth,
            ),
            patch(
                "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_connection",
                new=AsyncMock(),
            ),
            patch(
                "app.api.mcp_connection_cleanup.mcp_connection_store.delete",
                new=delete_row,
            ),
        )

    with ExitStack() as stack:
        for context in common_patches():
            stack.enter_context(context)
        stack.enter_context(pytest.raises(RuntimeError, match="secret unavailable"))
        await delete_mcp_connection(
            server_id=str(server.id),
            owner_id="user-1",
            workspace_id="ws-1",
            owner_type="user",
            membership_generation=1,
            _token_ok=None,
        )
    delete_row.assert_not_awaited()

    with ExitStack() as stack:
        for context in common_patches():
            stack.enter_context(context)
        await delete_mcp_connection(
            server_id=str(server.id),
            owner_id="user-1",
            workspace_id="ws-1",
            owner_type="user",
            membership_generation=1,
            _token_ok=None,
        )
    delete_row.assert_awaited_once_with("ws-1", str(server.id), owner)


@pytest.mark.anyio
async def test_user_cleanup_never_targets_workspace_installation_owner() -> None:
    seen_owners: list[ConnectionOwner] = []

    @asynccontextmanager
    async def owner_lock(_workspace_id, _server_id, owner):
        seen_owners.append(owner)
        yield

    with (
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.mutation_lock",
            new=owner_lock,
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.delete",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.delete_secret",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_token_service.delete_tokens",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_connection",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.purge_mcp_secrets",
            new=AsyncMock(),
        ),
    ):
        removed = await cleanup_user_server_connection(
            "ws-1",
            str(_server(credential_mode="workspace").id),
            "user-1",
        )
    assert removed is False
    assert seen_owners == [ConnectionOwner("user", "user-1")]


@pytest.mark.anyio
@pytest.mark.parametrize("oauth_issuer", [None, "https://issuer.example.test"])
async def test_terminal_user_cleanup_deletes_generic_and_oauth_secret_identities(
    oauth_issuer: str | None,
) -> None:
    server_id = str(_server().id)
    current = SimpleNamespace(
        workspace_id="ws-1",
        server_id=server_id,
        owner_type="user",
        owner_id="user-1",
        oauth_issuer=oauth_issuer,
    )
    delete_generic = AsyncMock()
    delete_oauth = AsyncMock()
    revoke = AsyncMock()
    delete_row = AsyncMock(return_value=True)
    purge = AsyncMock()
    with (
        patch(
                "app.api.mcp_connection_cleanup.mcp_connection_store.get",
            new=AsyncMock(return_value=current),
        ),
        patch(
                "app.api.mcp_connection_cleanup.mcp_connection_store.delete",
            new=delete_row,
        ),
        patch(
                "app.api.mcp_connection_cleanup.secret_store.delete_secret",
            new=delete_generic,
        ),
        patch(
                "app.api.mcp_connection_cleanup.oauth_token_service.delete_tokens",
            new=delete_oauth,
        ),
        patch(
                "app.api.mcp_connection_cleanup.oauth_token_service.revoke",
            new=revoke,
        ),
        patch(
                "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_connection",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.purge_mcp_secrets",
            new=purge,
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.count_mcp_secrets",
            new=AsyncMock(return_value=0),
        ),
    ):
        assert await cleanup_user_server_connection(
            "ws-1", server_id, "user-1"
        ) is True

    delete_generic.assert_awaited_once_with(
        credential_secret_name(
            "ws-1", server_id, ConnectionOwner("user", "user-1")
        ),
        {"workspace_id": "ws-1"},
    )
    delete_oauth.assert_awaited_once_with("ws-1", server_id, "user-1")
    if oauth_issuer is None:
        revoke.assert_not_awaited()
    else:
        revoke.assert_awaited_once()
    delete_row.assert_awaited_once()
    purge.assert_not_awaited()


@pytest.mark.anyio
async def test_terminal_user_cleanup_retains_row_when_second_secret_delete_fails() -> None:
    server_id = str(_server().id)
    current = SimpleNamespace(
        workspace_id="ws-1",
        server_id=server_id,
        owner_type="user",
        owner_id="user-1",
        oauth_issuer=None,
    )
    delete_row = AsyncMock(return_value=True)
    with (
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.get",
            new=AsyncMock(return_value=current),
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.delete",
            new=delete_row,
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_generic,
        patch(
            "app.api.mcp_connection_cleanup.oauth_token_service.delete_tokens",
            new=AsyncMock(side_effect=RuntimeError("secret backend unavailable")),
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_connection",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.purge_mcp_secrets",
            new=AsyncMock(),
        ),
        pytest.raises(RuntimeError, match="secret backend unavailable"),
    ):
        await cleanup_user_server_connection("ws-1", server_id, "user-1")

    delete_generic.assert_awaited_once()
    delete_row.assert_not_awaited()


@pytest.mark.anyio
async def test_server_cleanup_defensively_deletes_snapshotted_owner_secrets() -> None:
    server_id = str(_server().id)
    snapshot = SimpleNamespace(owner_type="user", owner_id="user-1")
    purge = AsyncMock()
    with (
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.list_for_server",
            new=AsyncMock(return_value=[snapshot]),
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.api.mcp_connection_cleanup.mcp_connection_store.delete",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.delete_secret",
            new=AsyncMock(),
        ) as delete_generic,
        patch(
            "app.api.mcp_connection_cleanup.oauth_token_service.delete_tokens",
            new=AsyncMock(),
        ) as delete_oauth,
        patch(
            "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_connection",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_flow_store.delete_for_server",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.oauth_registration_store.delete_for_server",
            new=AsyncMock(),
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.purge_mcp_secrets",
            new=purge,
        ),
        patch(
            "app.api.mcp_connection_cleanup.secret_store.count_mcp_secrets",
            new=AsyncMock(return_value=0),
        ),
    ):
        assert await cleanup_server_connections("ws-1", server_id) == 1

    delete_generic.assert_awaited_once()
    delete_oauth.assert_awaited_once_with("ws-1", server_id, "user-1")
    purge.assert_awaited_once_with("ws-1", server_id=server_id)
