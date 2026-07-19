from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.auth.claims import McpToolRef, TokenClaims
from app.examples import EXAMPLE_RUN_ID, EXAMPLE_TARGET_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.registry.store import ToolRegistry, tool_registry
from app.mcp.tool_identity import model_tool_alias
from app.target_types import KUBERNETES_TARGET_TYPE, TARGET_TYPE_EXAMPLES, TargetType


class ToolCallRequest(BaseModel):
    class Scope(BaseModel):
        type: Literal["target", "workspace"] = "target"

    run_id: str = Field(examples=[EXAMPLE_RUN_ID])
    workspace_id: str = Field(examples=[EXAMPLE_WORKSPACE_ID])
    scope: Scope = Field(default_factory=Scope)
    target_id: str | None = Field(default=None, examples=[EXAMPLE_TARGET_ID])
    target_type: TargetType | None = Field(default=None, examples=TARGET_TYPE_EXAMPLES)
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_session_id: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
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
            return self

        if self.agent_id and not self.workflow_id:
            if (self.target_id and not self.target_type) or (
                self.target_type and not self.target_id
            ):
                raise ValueError("agent target binding requires both target_id and target_type")
            return self
        missing = [
            name
            for name, value in (
                ("workflow_id", self.workflow_id),
                ("workflow_run_id", self.workflow_run_id),
                ("workflow_session_id", self.workflow_session_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"workspace workflow scope missing required fields: {', '.join(missing)}"
            )
        if (self.target_id and not self.target_type) or (
            self.target_type and not self.target_id
        ):
            raise ValueError("workflow target binding requires both target_id and target_type")
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
        return (
            req.workflow_id == claims.workflow_id
            and req.workflow_run_id == claims.workflow_run_id
            and req.workflow_session_id == claims.workflow_session_id
            and req.agent_id == claims.agent_id
            and req.agent_version == claims.agent_version
            and req.trigger_id == claims.trigger_id
            and req.target_id == claims.target_id
            and req.target_type == claims.target_type
        )
    return (
        req.target_id == claims.target_id
        and req.target_type == claims.target_type
        and req.agent_id == claims.agent_id
        and req.agent_version == claims.agent_version
    )


async def resolve_registered_tool(
    req: ToolCallRequest,
    *,
    target_id: str,
    target_type: str,
    registry: ToolRegistry = tool_registry,
):
    if req.tool_ref is None:
        return None
    tool = await registry.get_tool(
        req.workspace_id,
        target_id,
        req.tool_ref.tool_name,
        target_type=target_type,
        server_id=req.tool_ref.server_id,
    )
    if tool is None:
        return None
    expected_alias = model_tool_alias(str(tool.server_id), tool.tool_name)
    if req.tool != expected_alias and not (
        tool.source == "builtin" and req.tool == tool.tool_name
    ):
        return None
    return tool


def tool_ref_is_permitted(tool, req: ToolCallRequest, claims: TokenClaims) -> bool:
    return req.tool_ref is not None and any(
        ref.server_id == str(tool.server_id)
        and ref.tool_name == tool.tool_name
        and ref.server_id == req.tool_ref.server_id
        and ref.tool_name == req.tool_ref.tool_name
        for ref in claims.permissions.allowed_tool_refs
    )
