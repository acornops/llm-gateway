from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.target_types import TargetType


class NativeToolPermission(BaseModel):
    id: str
    config: dict[str, Any] = Field(default_factory=dict)


class McpToolRef(BaseModel):
    server_id: str
    tool_name: str


class RunPrincipalRef(BaseModel):
    type: Literal["user", "service_identity"]
    id: str


class Scope(BaseModel):
    type: Literal["target", "agent_chat", "workspace"] = "target"


class Permissions(BaseModel):
    allowed_providers: list[str] = []
    allowed_models: list[str] = []
    allowed_tools: list[str] = []
    allowed_tool_refs: list[McpToolRef] = []
    allowed_native_tools: list[NativeToolPermission] = []
    allowed_tool_operations: dict[str, Literal["read", "write"]] = {}
    max_output_tokens: int | None = None


class TokenClaims(BaseModel):
    iss: str
    aud: str
    iat: int
    exp: int
    sub: str
    user_id: str | None = None
    principal: RunPrincipalRef | None = None
    permission_mode: Literal[
        "read_only", "ask_before_changes", "auto_allowed_changes"
    ] = "ask_before_changes"
    run_id: str
    workspace_id: str
    scope: Scope = Scope()
    target_id: str | None = None
    target_type: TargetType | None = None
    workflow_id: str | None = None
    execution_id: str | None = None
    workflow_session_id: str | None = None
    executor_role: Literal["coordinator", "specialist"] | None = None
    agent_id: str | None = None
    trigger_id: str | None = None
    session_id: str
    permissions: Permissions

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.principal is None and self.user_id:
            self.principal = RunPrincipalRef(type="user", id=self.user_id)
        if self.principal is None:
            raise ValueError("run principal is required")
        if self.user_id and (
            self.principal.type != "user" or self.principal.id != self.user_id
        ):
            raise ValueError("user_id and principal do not match")
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
                    "target tokens forbid Agent and Workflow identity"
                )
            return self

        if self.scope.type == "agent_chat":
            if not self.agent_id:
                raise ValueError("agent chat tokens require agent identity")
            forbidden = (
                self.target_id,
                self.target_type,
                self.workflow_id,
                self.execution_id,
                self.workflow_session_id,
                self.executor_role,
                self.trigger_id,
            )
            if any(value is not None for value in forbidden):
                raise ValueError(
                    "agent chat tokens forbid target and workflow fields"
                )
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
            raise ValueError("workflow tokens forbid target identity claims")
        if self.executor_role == "coordinator" and self.agent_id:
            raise ValueError("coordinator workflow tokens forbid agent identity")
        if self.executor_role == "specialist" and not self.agent_id:
            raise ValueError("specialist workflow tokens require agent identity")
        return self
