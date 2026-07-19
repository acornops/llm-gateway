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


class AllowedRepository(BaseModel):
    provider: Literal["github", "gitlab"]
    repository: str
    ref: str | None = None
    change_request_number: int | None = Field(default=None, ge=1)


class Scope(BaseModel):
    type: Literal["target", "workspace"] = "target"


class Permissions(BaseModel):
    allowed_providers: list[str] = []
    allowed_models: list[str] = []
    allowed_tools: list[str] = []
    allowed_tool_refs: list[McpToolRef] = []
    allowed_native_tools: list[NativeToolPermission] = []
    allowed_tool_operations: dict[str, Literal["read", "write"]] = {}
    context_grants: list[str] = []
    allowed_repository: AllowedRepository | None = None
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
    workflow_run_id: str | None = None
    workflow_session_id: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
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
        if (self.target_id and not self.target_type) or (self.target_type and not self.target_id):
            raise ValueError("workflow target binding requires both target_id and target_type")
        return self
