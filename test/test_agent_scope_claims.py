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
from app.llm.service import Message, NormalizedLLMRequest


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
        agent_version=4,
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
        "agent_version": 4,
        "trigger_id": "trigger-manual-1",
        "session_id": "workflow-session-1",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "messages": [Message(role="user", content="hello")],
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
        "agent_version": 4,
        "trigger_id": "trigger-manual-1",
        "tool": "mcp.tools.list",
        "arguments": {},
    }
    payload.update(overrides)
    return ToolCallRequest(**payload)


def test_llm_workspace_scope_requires_matching_agent_claims():
    assert llm_request_matches_claim_scope(llm_request(), workspace_agent_claims())
    assert not llm_request_matches_claim_scope(
        llm_request(agent_version=5),
        workspace_agent_claims(),
    )


def test_tool_workspace_scope_requires_matching_agent_claims():
    assert tool_request_matches_claim_scope(tool_request(), workspace_agent_claims())
    assert not tool_request_matches_claim_scope(
        tool_request(agent_id="agent-release-coordinator"),
        workspace_agent_claims(),
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
            agent_version=4,
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
            agent_version=4,
            session_id="workflow-session-1",
            principal={"type": "user", "id": "user-1"},
            permissions=Permissions(),
        )
