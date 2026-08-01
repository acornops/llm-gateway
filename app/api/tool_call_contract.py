from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.auth.claims import McpToolRef, TokenClaims
from app.config.settings import settings
from app.examples import EXAMPLE_RUN_ID, EXAMPLE_TARGET_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.registry.store import ToolRegistry, mcp_server_registry, tool_registry
from app.mcp.tool_identity import model_tool_alias
from app.target_types import KUBERNETES_TARGET_TYPE, TARGET_TYPE_EXAMPLES, TargetType

TARGETS_MCP_SERVER_ID = "targets"


class ToolCallRequest(BaseModel):
    class Scope(BaseModel):
        type: Literal["target", "agent_chat", "workspace"] = "target"

    run_id: str = Field(examples=[EXAMPLE_RUN_ID])
    workspace_id: str = Field(examples=[EXAMPLE_WORKSPACE_ID])
    scope: Scope = Field(default_factory=Scope)
    target_id: str | None = Field(default=None, examples=[EXAMPLE_TARGET_ID])
    target_type: TargetType | None = Field(default=None, examples=TARGET_TYPE_EXAMPLES)
    workflow_id: str | None = None
    execution_id: str | None = None
    workflow_session_id: str | None = None
    executor_role: Literal["coordinator", "specialist"] | None = None
    agent_id: str | None = None
    trigger_id: str | None = None
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=256)
    approval_receipt: str | None = Field(default=None, min_length=1, max_length=8192)
    tool: str = Field(examples=["get_resource_logs"])
    tool_ref: McpToolRef | None = None
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.scope.type == "target":
            if not self.target_id or not self.target_type:
                raise ValueError("target scope requires target_id and target_type")
            forbidden = (
                self.workflow_id,
                self.execution_id,
                self.workflow_session_id,
                self.executor_role,
                self.agent_id,
                self.trigger_id,
            )
            if any(value is not None for value in forbidden):
                raise ValueError(
                    "target requests forbid Agent and Workflow identity"
                )
            return self

        if self.scope.type == "agent_chat":
            if not self.agent_id:
                raise ValueError("agent chat requests require agent identity")
            workflow_fields = (
                self.workflow_id,
                self.execution_id,
                self.workflow_session_id,
                self.executor_role,
                self.trigger_id,
            )
            if any(value is not None for value in workflow_fields):
                raise ValueError("agent chat requests forbid workflow fields")
            if self.target_id or self.target_type:
                raise ValueError("agent chat requests forbid target fields")
            return self

        missing = [
            name
            for name, value in (
                ("workflow_id", self.workflow_id),
                ("execution_id", self.execution_id),
                ("workflow_session_id", self.workflow_session_id),
                ("executor_role", self.executor_role),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"workspace workflow scope missing required fields: {', '.join(missing)}"
            )
        if self.target_id or self.target_type:
            raise ValueError("workspace workflow requests forbid target fields")
        if self.executor_role == "coordinator" and self.agent_id:
            raise ValueError("coordinator workflow requests forbid agent identity")
        if self.executor_role == "specialist" and not self.agent_id:
            raise ValueError("specialist workflow requests require agent identity")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "run_id": EXAMPLE_RUN_ID,
                "workspace_id": EXAMPLE_WORKSPACE_ID,
                "target_id": EXAMPLE_TARGET_ID,
                "target_type": KUBERNETES_TARGET_TYPE,
                "tool": "get_resource_logs",
                "arguments": {
                    "namespace": "payments",
                    "name": "payments-api-7f95b8f79-x2mhd",
                    "tail_lines": 200,
                },
            }
        }
    }


def request_matches_claim_scope(req: ToolCallRequest, claims: TokenClaims) -> bool:
    if req.run_id != claims.run_id or req.workspace_id != claims.workspace_id:
        return False
    if req.scope.type != claims.scope.type:
        return False
    if claims.scope.type == "workspace":
        workflow_scope_matches = (
            req.workflow_id == claims.workflow_id
            and req.execution_id == claims.execution_id
            and req.workflow_session_id == claims.workflow_session_id
            and req.executor_role == claims.executor_role
            and req.agent_id == claims.agent_id
            and req.trigger_id == claims.trigger_id
        )
        if not workflow_scope_matches:
            return False
        return workflow_scope_matches
    if claims.scope.type == "agent_chat":
        return req.agent_id == claims.agent_id
    return (
        req.target_id == claims.target_id
        and req.target_type == claims.target_type
    )


async def resolve_registered_tool(
    req: ToolCallRequest,
    *,
    destination_id: str,
    target_type: str | None = None,
    scope_type: str = "target",
    registry: ToolRegistry = tool_registry,
):
    if req.tool_ref is None:
        return None
    generic_target_ref = (
        req.scope.type in {"workspace", "agent_chat"}
        and req.tool_ref.server_id == TARGETS_MCP_SERVER_ID
    )
    server_id = req.tool_ref.server_id
    if generic_target_ref:
        server = await mcp_server_registry.get_server_by_url(
            req.workspace_id,
            destination_id,
            settings.BUILTIN_TARGET_MCP_SERVER_URL,
            target_type=target_type,
            scope_type="target",
        )
        if server is None or getattr(server, "provenance_type", "manual") != "builtin":
            return None
        server_id = str(server.id)
    registry_scope = (
        {"scope_type": "target", "target_type": target_type}
        if scope_type == "target"
        else {"scope_type": "agent"}
    )
    tool = await registry.get_tool(
        req.workspace_id,
        destination_id,
        req.tool_ref.tool_name,
        server_id=server_id,
        **registry_scope,
    )
    if tool is None:
        return None
    expected_alias = model_tool_alias(str(tool.server_id), tool.tool_name)
    if req.tool != expected_alias and not (tool.source == "builtin" and req.tool == tool.tool_name):
        return None
    return tool


def tool_ref_is_permitted(tool, req: ToolCallRequest, claims: TokenClaims) -> bool:
    if (
        req.tool_ref is not None
        and req.scope.type in {"workspace", "agent_chat"}
        and req.tool_ref.server_id == TARGETS_MCP_SERVER_ID
        and tool.source == "builtin"
    ):
        return any(
            ref.server_id == TARGETS_MCP_SERVER_ID
            and ref.tool_name == tool.tool_name == req.tool_ref.tool_name
            for ref in claims.permissions.allowed_tool_refs
        )
    return req.tool_ref is not None and any(
        ref.server_id == str(tool.server_id)
        and ref.tool_name == tool.tool_name
        and ref.server_id == req.tool_ref.server_id
        and ref.tool_name == req.tool_ref.tool_name
        for ref in claims.permissions.allowed_tool_refs
    )
