from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

SERVER_ID = "11111111-1111-4111-8111-111111111111"
SERVER_URL = "http://control-plane:8081/internal/v1/mcp"
TOOL_NAMES = ("list_targets", "get_target", "list_target_issues")


def registered_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        server_id=SERVER_ID,
        tool_name=name,
        mcp_server_url=SERVER_URL,
        target_type="agent",
        timeout_ms=10000,
        description=f"{name} description",
        capability="read",
        version="v1",
        source="builtin",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object"},
        artifact_policy="never",
        enabled=True,
        review_state="approved",
        risk_level="read_only",
        auto_allowed=False,
    )


@pytest.mark.anyio
async def test_create_agent_targets_builtin_server_uses_agent_scope_and_fixed_catalog() -> None:
    tools = [registered_tool(name) for name in TOOL_NAMES]
    server = SimpleNamespace(
        id=SERVER_ID,
        workspace_id="workspace-1",
        scope_type="agent",
        agent_id="agent-1",
        target_id="agent-1",
        target_type="agent",
        target_constraints={
            "target_types": ["kubernetes", "virtual_machine"],
            "target_ids": ["target-a", "target-b"],
        },
        server_name="acornops-targets",
        server_url=SERVER_URL,
        enabled=True,
        auth_type="none",
        credential_mode="none",
        auth_header_name=None,
        auth_header_prefix=None,
        public_headers=None,
        connection_status="unknown",
        last_discovery_at=None,
        last_discovery_error=None,
        provenance_type="builtin",
        endpoint_configuration={},
        revision=1,
    )

    with (
        patch(
            "app.api.handlers_mcp_admin.mcp_server_registry.create_server",
            new=AsyncMock(return_value=server),
        ) as create_server,
        patch(
            "app.api.handlers_mcp_admin.tool_registry.upsert_tool",
            new=AsyncMock(side_effect=tools),
        ) as upsert_tool,
        patch(
            "app.api.handlers_mcp_admin.tool_registry.list_target_tools",
            new=AsyncMock(return_value=tools),
        ),
        patch(
            "app.api.handlers_mcp_admin.mcp_transport.list_tools",
            new=AsyncMock(),
        ) as discover_tools,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/internal/mcp/servers",
                headers={"Authorization": "Bearer dev_orchestrator_token"},
                json={
                    "workspace_id": "workspace-1",
                    "scope_type": "agent",
                    "agent_id": "agent-1",
                    "target_constraints": {
                        "target_types": [
                            "virtual_machine",
                            "kubernetes",
                            "virtual_machine",
                        ],
                        "target_ids": ["target-b", "target-a", "target-a"],
                    },
                    "server_name": "acornops-targets",
                    "server_url": SERVER_URL,
                    "auth_type": "none",
                    "credential_mode": "none",
                    "tools": [
                        {
                            "name": name,
                            "source": "builtin",
                            "capability": "read",
                            "review_state": "approved",
                            "risk_level": "read_only",
                            "enabled": True,
                        }
                        for name in TOOL_NAMES
                    ],
                },
            )

    assert response.status_code == 201
    assert response.json()["scope_type"] == "agent"
    assert response.json()["agent_id"] == "agent-1"
    assert response.json()["target_id"] is None
    assert response.json()["target_type"] is None
    assert [tool["name"] for tool in response.json()["tools"]] == list(TOOL_NAMES)
    create_server.assert_awaited_once()
    assert create_server.await_args.kwargs["target_id"] == "agent-1"
    assert create_server.await_args.kwargs["target_type"] == "agent"
    assert create_server.await_args.kwargs["provenance_type"] == "builtin"
    assert create_server.await_args.kwargs["target_constraints"] == {
        "target_types": ["kubernetes", "virtual_machine"],
        "target_ids": ["target-a", "target-b"],
    }
    assert upsert_tool.await_count == 3
    assert {
        call.kwargs["tool_name"] for call in upsert_tool.await_args_list
    } == set(TOOL_NAMES)
    assert all(
        call.kwargs["source"] == "builtin" for call in upsert_tool.await_args_list
    )
    discover_tools.assert_not_awaited()
