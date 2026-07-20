import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.mcp.canonical_json import canonical_json
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


class ResourceBindingClaim(BaseModel):
    binding_id: str
    type: str
    resource_id: str
    provider: str
    provider_version: str
    workspace_id: str
    label_snapshot: str
    source: Literal["explicit", "implicit", "trigger"]
    operations: list[str]
    context_mode: Literal["inline", "tool", "routing_only"]
    provider_data: dict[str, Any] | None = None


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
    resource_bindings: list[ResourceBindingClaim] = Field(default_factory=list, max_length=64)
    binding_digest: str | None = None
    max_output_tokens: int | None = None

    @model_validator(mode="after")
    def validate_resource_bindings(self):
        binding_ids = [binding.binding_id for binding in self.resource_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("resource binding IDs must be unique")
        for binding in self.resource_bindings:
            if (
                not binding.operations
                or len(binding.operations) > 64
                or len(binding.operations) != len(set(binding.operations))
                or any(not operation.strip() for operation in binding.operations)
            ):
                raise ValueError(
                    "resource binding operations must be unique, non-empty, and bounded"
                )
        if self.binding_digest is not None and (
            len(self.binding_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.binding_digest)
        ):
            raise ValueError("binding_digest must be a lowercase SHA-256 hex string")
        if self.resource_bindings and self.binding_digest is None:
            raise ValueError("binding_digest is required when resource bindings are present")
        if self.binding_digest is not None:
            canonical = []
            for binding in self.resource_bindings:
                value = {
                    "bindingId": binding.binding_id,
                    "type": binding.type,
                    "resourceId": binding.resource_id,
                    "provider": binding.provider,
                    "providerVersion": binding.provider_version,
                    "workspaceId": binding.workspace_id,
                    "labelSnapshot": binding.label_snapshot,
                    "source": binding.source,
                    "operations": binding.operations,
                    "contextMode": binding.context_mode,
                }
                if binding.provider_data is not None:
                    value["providerData"] = binding.provider_data
                canonical.append(value)
            actual = hashlib.sha256(
                canonical_json(canonical).encode("utf-8")
            ).hexdigest()
            if actual != self.binding_digest:
                raise ValueError("binding_digest does not match resource_bindings")
        return self


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
        if any(
            binding.workspace_id != self.workspace_id
            for binding in self.permissions.resource_bindings
        ):
            raise ValueError("resource bindings must match the token workspace")
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
