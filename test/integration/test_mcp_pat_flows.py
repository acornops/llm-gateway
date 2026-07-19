import os
import ssl
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.claims import TokenClaims
from app.main import app
from app.mcp.tool_identity import model_tool_alias


@pytest.mark.anyio
async def test_target_and_agent_pat_connections_use_independent_header_formats() -> None:
    from app.auth.service_token import require_admin_service_token

    workspace_id = f"ws-pat-{uuid4()}"
    user_id = f"user-{uuid4()}"
    base_url = os.getenv(
        "INTEGRATION_MCP_URL", "https://localhost:8002/mcp"
    ).rstrip("/")
    parsed_mcp_url = urlsplit(base_url)
    mock_control_origin = f"{parsed_mcp_url.scheme}://{parsed_mcp_url.netloc}"
    ca_bundle_file = os.getenv("MCP_EGRESS_CA_BUNDLE_FILE", "").strip()
    mock_control_verify = (
        ssl.create_default_context(cafile=ca_bundle_file) if ca_bundle_file else True
    )
    installations = [
        {
            "scope_type": "target",
            "target_id": f"target-{uuid4()}",
            "target_type": "kubernetes",
            "server_url": f"{base_url}/bearer",
            "auth_type": "bearer_token",
            "auth_header_name": "Authorization",
            "auth_header_prefix": "Bearer ",
            "credential": "bearer-pat",
        },
        {
            "scope_type": "agent",
            "agent_id": f"agent-{uuid4()}",
            "server_url": f"{base_url}/custom",
            "auth_type": "custom_header",
            "auth_header_name": "X-Mcp-Pat",
            "auth_header_prefix": "",
            "credential": "custom-pat",
        },
    ]
    app.dependency_overrides[require_admin_service_token] = lambda: None

    created: list[dict[str, str]] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        try:
            async with AsyncClient(
                base_url=mock_control_origin, verify=mock_control_verify
            ) as mock_control:
                reset = await mock_control.post("/control/reset")
                assert reset.status_code == 200, reset.text
            for index, installation in enumerate(installations):
                server_input = {
                    key: value
                    for key, value in installation.items()
                    if key != "credential"
                }
                response = await ac.post(
                    "/api/v1/internal/mcp/servers",
                    json={
                        "workspace_id": workspace_id,
                        "server_name": f"PAT integration {index}",
                        "enabled": True,
                        "auth_scope": "personal",
                        "tools": [],
                        **server_input,
                    },
                )
                assert response.status_code == 201, response.text
                created.append(response.json())

            for server, installation in zip(created, installations, strict=True):
                connected = await ac.put(
                    f"/api/v1/internal/mcp/servers/{server['id']}/connections/{user_id}",
                    json={
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "credential": installation["credential"],
                        "consent_granted": True,
                    },
                )
                assert connected.status_code == 200, connected.text
                assert connected.json() == {
                    "server_id": server["id"],
                    "status": "connected",
                    "auth_type": installation["auth_type"],
                    "action": None,
                }

            for server, installation in zip(created, installations, strict=True):
                is_agent = installation["scope_type"] == "agent"
                target_id = (
                    installation["agent_id"]
                    if is_agent
                    else installation["target_id"]
                )
                tool_query = {
                    "workspace_id": workspace_id,
                    "target_id": target_id,
                    "target_type": "agent" if is_agent else installation["target_type"],
                    "scope_type": "agent" if is_agent else "target",
                    "server_id": server["id"],
                }
                if is_agent:
                    tool_query["agent_id"] = installation["agent_id"]
                approved = await ac.patch(
                    "/api/v1/internal/mcp/tools/get_weather",
                    params=tool_query,
                    json={
                        "enabled": True,
                        "capability": "read",
                        "review_state": "approved",
                        "risk_level": "read_only",
                    },
                )
                assert approved.status_code == 200, approved.text

            from app.auth.jwt_validator import validator

            for server, installation in zip(created, installations, strict=True):
                is_agent = installation["scope_type"] == "agent"
                target_id = (
                    installation["agent_id"]
                    if is_agent
                    else installation["target_id"]
                )
                tool_alias = model_tool_alias(server["id"], "get_weather")
                claims = TokenClaims(
                    iss="llm-gateway",
                    aud="execution-gateway",
                    iat=1,
                    exp=4_102_444_800,
                    sub=user_id,
                    user_id=user_id,
                    permission_mode="read_only",
                    run_id=f"run-{uuid4()}",
                    workspace_id=workspace_id,
                    scope={"type": "workspace" if is_agent else "target"},
                    target_id=None if is_agent else target_id,
                    target_type=None if is_agent else installation["target_type"],
                    agent_id=installation.get("agent_id"),
                    agent_version=1 if is_agent else None,
                    session_id=f"session-{uuid4()}",
                    permissions={
                        "allowed_tools": ["*"],
                        "allowed_tool_refs": [
                            {"server_id": server["id"], "tool_name": "get_weather"}
                        ],
                    },
                )

                async def override_validate(current_claims=claims):
                    return current_claims

                app.dependency_overrides[validator.validate] = override_validate
                auth_mode = "custom" if is_agent else "bearer"
                async with AsyncClient(
                    base_url=mock_control_origin, verify=mock_control_verify
                ) as mock_control:
                    reset = await mock_control.post("/control/reset")
                    assert reset.status_code == 200, reset.text
                    revoked = await mock_control.post(f"/control/revoke/{auth_mode}")
                    assert revoked.status_code == 200, revoked.text

                    payload = {
                        "run_id": claims.run_id,
                        "workspace_id": workspace_id,
                        "scope": {"type": "workspace" if is_agent else "target"},
                        "target_id": None if is_agent else target_id,
                        "target_type": None if is_agent else installation["target_type"],
                        "agent_id": installation.get("agent_id"),
                        "agent_version": 1 if is_agent else None,
                        "tool": tool_alias,
                        "tool_ref": {"server_id": server["id"], "tool_name": "get_weather"},
                        "arguments": {"location": "Singapore"},
                    }
                    first = await ac.post(
                        "/api/v1/mcp/tool-call",
                        json=payload,
                        headers={"Authorization": "Bearer run-scoped-jwt"},
                    )
                    assert first.status_code == 200, first.text
                    assert first.json()["full_result"]["code"] == "MCP_AUTHENTICATION_FAILED"

                    connection = await ac.get(
                        f"/api/v1/internal/mcp/servers/{server['id']}/connections/{user_id}",
                        params={"workspace_id": workspace_id},
                    )
                    assert connection.status_code == 200, connection.text
                    assert connection.json()["status"] == "error"

                    after_first = (await mock_control.get("/control/stats")).json()
                    second = await ac.post(
                        "/api/v1/mcp/tool-call",
                        json=payload,
                        headers={"Authorization": "Bearer run-scoped-jwt"},
                    )
                    after_second = (await mock_control.get("/control/stats")).json()
                    assert second.status_code == 409, second.text
                    assert second.json()["detail"]["action"] == "verify_mcp_server"
                    assert after_second[auth_mode]["requests"] == after_first[auth_mode]["requests"]

            rotated = await ac.put(
                f"/api/v1/internal/mcp/servers/{created[0]['id']}/connections/{user_id}",
                json={
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "credential": "revoked-pat",
                    "consent_granted": True,
                },
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["status"] == "error"
            assert rotated.json()["action"] == "verify_mcp_server"

            retry = await ac.post(
                f"/api/v1/internal/mcp/servers/{created[0]['id']}/connections/{user_id}/verify",
                json={"workspace_id": workspace_id, "user_id": user_id},
            )
            assert retry.status_code == 200, retry.text
            assert retry.json()["status"] == "error"

            replaced = await ac.put(
                f"/api/v1/internal/mcp/servers/{created[0]['id']}/connections/{user_id}",
                json={
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "credential": "bearer-pat",
                    "consent_granted": True,
                },
            )
            assert replaced.status_code == 200, replaced.text
            assert replaced.json()["status"] == "connected"
        finally:
            for server, installation in zip(created, installations, strict=False):
                is_agent = installation["scope_type"] == "agent"
                await ac.delete(
                    f"/api/v1/internal/mcp/servers/{server['id']}/connections/{user_id}",
                    params={"workspace_id": workspace_id},
                )
                await ac.delete(
                    f"/api/v1/internal/mcp/servers/{server['id']}",
                    params={
                        "workspace_id": workspace_id,
                        "target_id": (
                            installation["agent_id"]
                            if is_agent
                            else installation["target_id"]
                        ),
                        "target_type": (
                            "agent" if is_agent else installation["target_type"]
                        ),
                    },
                )
            app.dependency_overrides.clear()
