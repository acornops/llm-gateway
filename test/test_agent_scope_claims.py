import pytest
from pydantic import ValidationError

from app.api.handlers_llm_stream import (
    _request_matches_claim_scope as llm_request_matches_claim_scope,
)
from app.api.tool_call_contract import (
    ToolCallRequest,
)
from app.api.tool_call_contract import (
    request_matches_claim_scope as tool_request_matches_claim_scope,
)
from app.auth.claims import Permissions, TokenClaims
from app.llm.service import NormalizedLLMRequest


def workspace_agent_claims() -> TokenClaims:
    return TokenClaims(
        iss="issuer",
        aud="audience",
        iat=1,
        exp=999,
        sub="run:run-1",
        user_id="user-1",
        run_id="run-1",
        workspace_id="ws-1",
        scope={"type": "workspace"},
        workflow_id="workflow-1",
        execution_id="workflow-execution-1",
        workflow_session_id="workflow-session-1",
        executor_role="specialist",
        agent_id="agent-cluster-triage",
        trigger_id="trigger-manual-1",
        session_id="workflow-session-1",
        permissions=Permissions(allowed_tools=["mcp.tools.list"]),
    )


def llm_request(**overrides) -> NormalizedLLMRequest:
    payload = {
        "run_id": "run-1",
        "workspace_id": "ws-1",
        "scope": {"type": "workspace"},
        "workflow_id": "workflow-1",
        "execution_id": "workflow-execution-1",
        "workflow_session_id": "workflow-session-1",
        "executor_role": "specialist",
        "agent_id": "agent-cluster-triage",
        "trigger_id": "trigger-manual-1",
        "session_id": "workflow-session-1",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "runtime_instruction": "You are AcornOps.",
        "transcript": [{"type": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return NormalizedLLMRequest(**payload)


def tool_request(**overrides) -> ToolCallRequest:
    payload = {
        "run_id": "run-1",
        "workspace_id": "ws-1",
        "scope": {"type": "workspace"},
        "workflow_id": "workflow-1",
        "execution_id": "workflow-execution-1",
        "workflow_session_id": "workflow-session-1",
        "executor_role": "specialist",
        "agent_id": "agent-cluster-triage",
        "trigger_id": "trigger-manual-1",
        "tool": "mcp.tools.list",
        "arguments": {},
    }
    payload.update(overrides)
    return ToolCallRequest(**payload)


def agent_chat_claims(**overrides) -> TokenClaims:
    payload = {
        "iss": "issuer",
        "aud": "audience",
        "iat": 1,
        "exp": 999,
        "sub": "run:agent-chat-run-1",
        "user_id": "user-1",
        "run_id": "agent-chat-run-1",
        "workspace_id": "ws-1",
        "scope": {"type": "agent_chat"},
        "agent_id": "agent-cluster-triage",
        "session_id": "agent-conversation-1",
        "permissions": Permissions(allowed_tools=["mcp.tools.list"]),
    }
    payload.update(overrides)
    return TokenClaims(**payload)


def agent_chat_llm_request(**overrides) -> NormalizedLLMRequest:
    payload = {
        "run_id": "agent-chat-run-1",
        "workspace_id": "ws-1",
        "scope": {"type": "agent_chat"},
        "agent_id": "agent-cluster-triage",
        "session_id": "agent-conversation-1",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "runtime_instruction": "You are AcornOps.",
        "transcript": [{"type": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return NormalizedLLMRequest(**payload)


def agent_chat_tool_request(**overrides) -> ToolCallRequest:
    payload = {
        "run_id": "agent-chat-run-1",
        "workspace_id": "ws-1",
        "scope": {"type": "agent_chat"},
        "agent_id": "agent-cluster-triage",
        "tool": "mcp.tools.list",
        "arguments": {},
    }
    payload.update(overrides)
    return ToolCallRequest(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_id", "agent-1"),
        ("workflow_id", "workflow-1"),
        ("execution_id", "execution-1"),
        ("workflow_session_id", "workflow-session-1"),
        ("executor_role", "coordinator"),
        ("trigger_id", "trigger-1"),
    ],
)
def test_target_claims_and_requests_reject_agent_or_workflow_identity(
    field: str,
    value: str,
):
    base_claims = {
        "iss": "issuer",
        "aud": "audience",
        "iat": 1,
        "exp": 999,
        "sub": "run:target-run-1",
        "user_id": "user-1",
        "run_id": "target-run-1",
        "workspace_id": "ws-1",
        "target_id": "cluster-1",
        "target_type": "kubernetes",
        "session_id": "target-session-1",
        "permissions": Permissions(),
        field: value,
    }
    with pytest.raises(ValidationError, match="forbid Agent and Workflow"):
        TokenClaims(**base_claims)

    base_tool_request = {
        "run_id": "target-run-1",
        "workspace_id": "ws-1",
        "target_id": "cluster-1",
        "target_type": "kubernetes",
        "tool": "read",
        "arguments": {},
        field: value,
    }
    with pytest.raises(ValidationError, match="forbid Agent and Workflow"):
        ToolCallRequest(**base_tool_request)

    base_llm_request = {
        "run_id": "target-run-1",
        "workspace_id": "ws-1",
        "target_id": "cluster-1",
        "target_type": "kubernetes",
        "session_id": "target-session-1",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "runtime_instruction": "You are AcornOps.",
        "transcript": [{"type": "user", "content": "hello"}],
        field: value,
    }
    with pytest.raises(ValidationError, match="forbid Agent and Workflow"):
        NormalizedLLMRequest(**base_llm_request)


def test_llm_workspace_scope_requires_matching_agent_claims():
    assert llm_request_matches_claim_scope(llm_request(), workspace_agent_claims())
    assert not llm_request_matches_claim_scope(
        llm_request(agent_id="agent-other"),
        workspace_agent_claims(),
    )


def test_tool_workspace_scope_requires_matching_agent_claims():
    assert tool_request_matches_claim_scope(tool_request(), workspace_agent_claims())
    assert not tool_request_matches_claim_scope(
        tool_request(agent_id="agent-release-coordinator"),
        workspace_agent_claims(),
    )


def test_workspace_llm_requests_reject_target_identity_fields():
    with pytest.raises(ValidationError, match="forbid target identity fields"):
        llm_request(target_id="vm-1", target_type="virtual_machine")


def test_agent_chat_scope_matches_exact_agent_without_workflow_identity():
    claims = agent_chat_claims()
    assert llm_request_matches_claim_scope(agent_chat_llm_request(), claims)
    assert tool_request_matches_claim_scope(agent_chat_tool_request(), claims)
    assert not llm_request_matches_claim_scope(
        agent_chat_llm_request(agent_id="agent-other"), claims
    )
    assert not tool_request_matches_claim_scope(
        agent_chat_tool_request(agent_id="agent-other"), claims
    )


def test_agent_chat_scope_allows_any_call_time_target_through_signed_targets_mcp_ref():
    claims = agent_chat_claims(
        permissions={
            "allowed_tools": ["read"],
            "allowed_tool_refs": [{"server_id": "targets", "tool_name": "read"}],
        }
    )
    assert tool_request_matches_claim_scope(
        agent_chat_tool_request(
            tool="read",
            tool_ref={"server_id": "targets", "tool_name": "read"},
            arguments={"target_id": "vm-1", "target_type": "virtual_machine"},
        ),
        claims,
    )
    assert tool_request_matches_claim_scope(
        agent_chat_tool_request(
            tool="read",
            tool_ref={"server_id": "targets", "tool_name": "read"},
            arguments={"target_id": "vm-2", "target_type": "virtual_machine"},
        ),
        claims,
    )


def test_agent_chat_claims_reject_workflow_or_target_binding_fields():
    with pytest.raises(ValidationError, match="forbid target and workflow fields"):
        agent_chat_claims(target_id="cluster-1", target_type="kubernetes")
    with pytest.raises(ValidationError, match="forbid target and workflow fields"):
        agent_chat_claims(workflow_id="workflow-1")


def test_agent_chat_tool_requests_reject_target_binding_fields():
    with pytest.raises(ValidationError, match="forbid target fields"):
        agent_chat_tool_request(
            target_id="vm-1",
            target_type="virtual_machine",
        )


def test_workspace_claims_reject_persistent_target_binding():
    with pytest.raises(ValidationError, match="forbid target identity claims"):
        TokenClaims(
            **{
                **workspace_agent_claims().model_dump(),
                "target_id": "cluster-1",
                "target_type": "kubernetes",
            }
        )


def test_tool_workspace_scope_allows_any_call_time_target_through_signed_targets_mcp_ref():
    claims = TokenClaims(
        **{
            **workspace_agent_claims().model_dump(exclude={"permissions"}),
            "permissions": {
                "allowed_tools": ["read"],
                "allowed_tool_refs": [
                    {"server_id": "targets", "tool_name": "read"}
                ],
            },
        }
    )

    assert tool_request_matches_claim_scope(
        tool_request(
            tool="read",
            tool_ref={"server_id": "targets", "tool_name": "read"},
            arguments={"target_id": "vm-1", "target_type": "virtual_machine"},
        ),
        claims,
    )
    assert tool_request_matches_claim_scope(
        tool_request(
            tool="read",
            tool_ref={"server_id": "targets", "tool_name": "read"},
            arguments={"target_id": "vm-2", "target_type": "virtual_machine"},
        ),
        claims,
    )


def test_workspace_scope_rejects_agent_without_workflow():
    with pytest.raises(ValidationError, match="workspace workflow scope missing required fields"):
        TokenClaims(
            iss="issuer",
            aud="audience",
            iat=1,
            exp=999,
            sub="run:run-1",
            run_id="run-1",
            workspace_id="ws-1",
            scope={"type": "workspace"},
            agent_id="agent-cluster-triage",
            session_id="session-1",
            principal={"type": "user", "id": "user-1"},
            permissions=Permissions(),
        )


def test_coordinator_scope_rejects_agent_identity():
    with pytest.raises(ValidationError, match="coordinator workflow tokens forbid agent identity"):
        TokenClaims(
            iss="issuer",
            aud="audience",
            iat=1,
            exp=999,
            sub="run:run-1",
            run_id="run-1",
            workspace_id="ws-1",
            scope={"type": "workspace"},
            workflow_id="workflow-1",
            execution_id="execution-1",
            workflow_session_id="workflow-session-1",
            executor_role="coordinator",
            agent_id="agent-cluster-triage",
            session_id="workflow-session-1",
            principal={"type": "user", "id": "user-1"},
            permissions=Permissions(),
        )
