import json
from unittest.mock import AsyncMock, patch

import pytest

from app.scripts.mcp_user_lifecycle_preflight import (
    UserLifecyclePreflight,
    _report,
    _run,
)


def test_preflight_reports_individual_reset_and_unaffected_workspace_counts(capsys) -> None:
    _report(
        UserLifecyclePreflight(
            individual_connection_count=5,
            individual_owner_count=3,
            workspace_connection_count=4,
            unbound_individual_connection_count=5,
            mcp_server_count=6,
        )
    )

    assert json.loads(capsys.readouterr().out) == {
        "individualConnectionResetCount": 5,
        "individualOwnerResetCount": 3,
        "individualSecretObjectCount": 0,
        "knownOwnerServerCleanupUpperBound": 18,
        "mcpServerCount": 6,
        "oauthFlowRecordCount": 0,
        "unboundIndividualConnectionCount": 5,
        "workspaceOwnedConnectionCountUnaffected": 4,
        "workspaceOwnedSecretObjectCountUnaffected": 0,
    }


@pytest.mark.anyio
async def test_preflight_acceptance_requires_empty_individual_reset_state() -> None:
    with patch(
        "app.scripts.mcp_user_lifecycle_preflight._load_preflight",
        new=AsyncMock(
            return_value=UserLifecyclePreflight(
                individual_connection_count=1,
                individual_owner_count=1,
                workspace_connection_count=2,
                unbound_individual_connection_count=1,
                mcp_server_count=1,
            )
        ),
    ):
        assert await _run(fail_on_unbound=True) == 2

    with patch(
        "app.scripts.mcp_user_lifecycle_preflight._load_preflight",
        new=AsyncMock(
            return_value=UserLifecyclePreflight(
                individual_connection_count=0,
                individual_owner_count=0,
                workspace_connection_count=2,
                unbound_individual_connection_count=0,
                mcp_server_count=1,
                individual_secret_object_count=1,
                oauth_flow_record_count=1,
            )
        ),
    ):
        assert await _run(fail_on_unbound=True) == 2

    with patch(
        "app.scripts.mcp_user_lifecycle_preflight._load_preflight",
        new=AsyncMock(
            return_value=UserLifecyclePreflight(
                individual_connection_count=0,
                individual_owner_count=0,
                workspace_connection_count=2,
                unbound_individual_connection_count=0,
                mcp_server_count=1,
            )
        ),
    ):
        assert await _run(fail_on_unbound=True) == 0


@pytest.mark.anyio
async def test_apply_reset_purges_global_user_secrets_and_oauth_flows() -> None:
    before = UserLifecyclePreflight(
        individual_connection_count=1,
        individual_owner_count=1,
        workspace_connection_count=2,
        unbound_individual_connection_count=0,
        mcp_server_count=1,
        individual_secret_object_count=3,
        oauth_flow_record_count=2,
    )
    after = UserLifecyclePreflight(
        individual_connection_count=0,
        individual_owner_count=0,
        workspace_connection_count=2,
        unbound_individual_connection_count=0,
        mcp_server_count=1,
    )
    with (
        patch(
            "app.scripts.mcp_user_lifecycle_preflight._load_preflight",
            new=AsyncMock(side_effect=[before, after]),
        ),
        patch(
            "app.scripts.mcp_user_lifecycle_preflight.secret_store.purge_all_mcp_user_secrets",
            new=AsyncMock(return_value=3),
        ) as purge_secrets,
        patch(
            "app.scripts.mcp_user_lifecycle_preflight.oauth_flow_store.purge_all_flow_state",
            new=AsyncMock(return_value=2),
        ) as purge_flows,
        patch(
            "app.scripts.mcp_user_lifecycle_preflight._load_user_connection_reset_rows",
            new=AsyncMock(return_value=[("ws-1", "server-1", "user-1")]),
        ),
        patch(
            "app.scripts.mcp_user_lifecycle_preflight.cleanup_user_server_connection",
            new=AsyncMock(return_value=True),
        ) as cleanup_connection,
    ):
        assert (
            await _run(
                fail_on_unbound=False,
                apply_individual_reset=True,
            )
            == 0
        )

    purge_secrets.assert_awaited_once()
    purge_flows.assert_awaited_once()
    cleanup_connection.assert_awaited_once_with(
        "ws-1",
        "server-1",
        "user-1",
        reason="rollout_individual_reset",
    )


@pytest.mark.anyio
async def test_apply_reset_fails_acceptance_when_secret_state_remains() -> None:
    state = UserLifecyclePreflight(
        individual_connection_count=0,
        individual_owner_count=0,
        workspace_connection_count=1,
        unbound_individual_connection_count=0,
        mcp_server_count=1,
        individual_secret_object_count=1,
    )
    with (
        patch(
            "app.scripts.mcp_user_lifecycle_preflight._load_preflight",
            new=AsyncMock(side_effect=[state, state]),
        ),
        patch(
            "app.scripts.mcp_user_lifecycle_preflight.secret_store.purge_all_mcp_user_secrets",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.scripts.mcp_user_lifecycle_preflight.oauth_flow_store.purge_all_flow_state",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.scripts.mcp_user_lifecycle_preflight._load_user_connection_reset_rows",
            new=AsyncMock(return_value=[]),
        ),
    ):
        assert (
            await _run(
                fail_on_unbound=False,
                apply_individual_reset=True,
            )
            == 2
        )
