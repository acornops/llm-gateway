import unicodedata
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.examples import EXAMPLE_WORKSPACE_ID
from app.internal_model_tools import is_reserved_internal_tool_name
from app.mcp.header_policy import (
    validate_auth_header_name,
    validate_auth_header_value,
    validate_public_headers,
)
from app.target_types import KUBERNETES_TARGET_TYPE, TARGET_TYPE_EXAMPLES, TargetType

McpScopeType = Literal["agent", "target"]
McpRegistryTargetType = TargetType | Literal["agent"]


class AgentTargetConstraints(BaseModel):
    target_types: list[TargetType] = Field(default_factory=list, max_length=16)
    target_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("target_types", "target_ids")
    @classmethod
    def _deduplicate_constraints(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})

    model_config = ConfigDict(extra="forbid")


def _effective_auth_header_prefix(auth_type: str | None, header_prefix: str | None) -> str:
    if auth_type == "bearer_token":
        return "Bearer "
    return header_prefix or ""


class ToolConfigRequest(BaseModel):
    name: str = Field(min_length=1, examples=["records.list"])
    timeout_ms: int = Field(default=10000, ge=100, le=120000)
    description: str | None = None
    capability: Literal["read", "write"] = "write"
    version: str = "v1"
    source: Literal["mcp", "builtin"] = "mcp"
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    artifact_policy: Literal["never", "if_detailed", "always"] = "never"
    enabled: bool = True
    review_state: Literal["pending", "approved", "rejected"] = "pending"
    risk_level: Literal[
        "read_only", "non_destructive_write", "high_risk", "destructive"
    ] = "high_risk"
    auto_allowed: bool = False

    @field_validator("name")
    @classmethod
    def _validate_tool_name(cls, value: str) -> str:
        tool_name = value.strip()
        if not tool_name:
            raise ValueError("tool name is required")
        if is_reserved_internal_tool_name(tool_name):
            raise ValueError("tool name is reserved by the platform")
        return tool_name

    @model_validator(mode="after")
    def _validate_review(self) -> Self:
        if self.auto_allowed and self.risk_level != "non_destructive_write":
            raise ValueError("only non-destructive writes may be auto allowed")
        if self.source == "mcp" and self.enabled and self.review_state != "approved":
            raise ValueError("MCP tools must be approved before they are enabled")
        return self

    model_config = ConfigDict(extra="forbid", strict=True)


class ToolConfigUpdateRequest(BaseModel):
    """Partial tool configuration for the current server update endpoint."""

    name: str = Field(min_length=1, examples=["records.list"])
    timeout_ms: int | None = Field(default=None, ge=100, le=120000)
    description: str | None = None
    capability: Literal["read", "write"] | None = None
    version: str | None = None
    source: Literal["mcp", "builtin"] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    artifact_policy: Literal["never", "if_detailed", "always"] | None = None
    enabled: bool | None = None
    review_state: Literal["pending", "approved", "rejected"] | None = None
    risk_level: Literal[
        "read_only", "non_destructive_write", "high_risk", "destructive"
    ] | None = None
    auto_allowed: bool | None = None

    @field_validator("name")
    @classmethod
    def _validate_tool_name(cls, value: str) -> str:
        tool_name = value.strip()
        if not tool_name:
            raise ValueError("tool name is required")
        if is_reserved_internal_tool_name(tool_name):
            raise ValueError("tool name is reserved by the platform")
        return tool_name

    @model_validator(mode="after")
    def _validate_auto_allowed(self) -> Self:
        if self.auto_allowed and self.risk_level not in (None, "non_destructive_write"):
            raise ValueError("only non-destructive writes may be auto allowed")
        return self

    model_config = ConfigDict(extra="forbid", strict=True)


class ToolConfigResponse(BaseModel):
    name: str
    server_id: str
    model_alias: str
    mcp_server_url: str
    timeout_ms: int
    description: str | None = None
    capability: Literal["read", "write"] = "write"
    version: str = "v1"
    source: Literal["mcp", "builtin"] = "mcp"
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    artifact_policy: Literal["never", "if_detailed", "always"] = "never"
    enabled: bool
    review_state: Literal["pending", "approved", "rejected"] = "pending"
    risk_level: Literal[
        "read_only", "non_destructive_write", "high_risk", "destructive"
    ] = "high_risk"
    auto_allowed: bool = False


class ToolUpdateRequest(BaseModel):
    enabled: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=120000)
    description: str | None = None
    capability: Literal["read", "write"] | None = None
    version: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    artifact_policy: Literal["never", "if_detailed", "always"] | None = None
    review_state: Literal["pending", "approved", "rejected"] | None = None
    risk_level: Literal[
        "read_only", "non_destructive_write", "high_risk", "destructive"
    ] | None = None
    auto_allowed: bool | None = None

    @model_validator(mode="after")
    def _validate_auto_allowed(self) -> Self:
        if self.auto_allowed and self.risk_level not in (None, "non_destructive_write"):
            raise ValueError("only non-destructive writes may be auto allowed")
        return self

    model_config = ConfigDict(extra="forbid", strict=True)


class McpServerCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, examples=[EXAMPLE_WORKSPACE_ID])
    scope_type: McpScopeType = "target"
    agent_id: str | None = Field(default=None, min_length=1)
    target_id: str | None = Field(default=None, min_length=1)
    target_type: McpRegistryTargetType | None = Field(default=None, examples=TARGET_TYPE_EXAMPLES)
    target_constraints: AgentTargetConstraints = Field(default_factory=AgentTargetConstraints)
    server_name: str = Field(min_length=1, examples=["operations-catalog"])
    server_url: str = Field(min_length=1, examples=["https://mcp.example.com/v1/"])
    enabled: bool = True
    auth_type: Literal["none", "bearer_token", "custom_header"] = "none"
    credential_mode: Literal["none", "workspace", "individual"] = "none"
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    public_headers: dict[str, str] | None = None
    tools: list[ToolConfigRequest] = Field(default_factory=list)
    expected_absent: bool = True

    @field_validator("public_headers")
    @classmethod
    def _validate_public_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_public_headers(value)

    @field_validator("auth_header_name")
    @classmethod
    def _validate_auth_header_name(cls, value: str | None) -> str | None:
        return validate_auth_header_name(value)

    @field_validator("auth_header_prefix")
    @classmethod
    def _validate_auth_header_value(cls, value: str | None) -> str | None:
        return validate_auth_header_value(value)

    @model_validator(mode="after")
    def _validate_auth_config(self) -> Self:
        if self.scope_type == "agent":
            if not self.agent_id:
                raise ValueError("agent scope requires agent_id")
            if self.target_id or self.target_type:
                raise ValueError("agent scope does not accept target_id or target_type")
            # The current registry schema retains an internal owner key while
            # the public contract exposes only agent_id for Agent records.
            self.target_id = self.agent_id
            self.target_type = "agent"
        elif not self.target_id or self.target_type in (None, "agent"):
            raise ValueError("target scope requires target_id and a concrete target_type")
        external_authenticated = self.auth_type in ("bearer_token", "custom_header")
        if external_authenticated and self.credential_mode == "none":
            raise ValueError("authenticated MCP installations require a credential mode")
        if not external_authenticated and self.credential_mode != "none":
            raise ValueError("credential_mode must be none when auth_type is none")
        if self.auth_type == "none" and any(
            (
                self.auth_header_name,
                self.auth_header_prefix,
            )
        ):
            raise ValueError("auth fields are not allowed when auth_type is none")
        if self.auth_type == "custom_header" and not self.auth_header_name:
            raise ValueError("auth_header_name is required for custom_header auth")
        return self

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "workspace_id": EXAMPLE_WORKSPACE_ID,
                "target_id": "5b006e4c-509c-458a-9f02-5aafbdc01ade",
                "target_type": KUBERNETES_TARGET_TYPE,
                "server_name": "operations-catalog",
                "server_url": "https://mcp.example.com/v1/",
                "enabled": True,
                "public_headers": {"x-client-version": "2026-05"},
                "auth_type": "bearer_token",
                "credential_mode": "individual",
                "auth_header_name": "Authorization",
                "auth_header_prefix": "Bearer ",
                "tools": [
                    {
                        "name": "records.list",
                        "timeout_ms": 10000,
                        "enabled": True,
                    }
                ],
            }
        },
    )


class McpServerUpdateRequest(BaseModel):
    server_url: str | None = Field(default=None, min_length=1)
    server_name: str | None = None
    enabled: bool | None = None
    auth_type: Literal["none", "bearer_token", "custom_header"] | None = None
    credential_mode: Literal["none", "workspace", "individual"] | None = None
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    public_headers: dict[str, str] | None = None
    tools: list[ToolConfigUpdateRequest] | None = None
    remove_tools: list[str] = Field(default_factory=list)
    target_constraints: AgentTargetConstraints | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("public_headers")
    @classmethod
    def _validate_public_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_public_headers(value)

    @field_validator("auth_header_name")
    @classmethod
    def _validate_auth_header_name(cls, value: str | None) -> str | None:
        return validate_auth_header_name(value)

    @field_validator("auth_header_prefix")
    @classmethod
    def _validate_auth_header_value(cls, value: str | None) -> str | None:
        return validate_auth_header_value(value)

    @model_validator(mode="after")
    def _validate_constructed_auth_header_value(self) -> Self:
        if self.auth_type == "none" and any(
            (
                self.auth_header_name,
                self.auth_header_prefix,
            )
        ):
            raise ValueError("auth fields are not allowed when auth_type is none")
        return self

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "enabled": True,
                "public_headers": {"x-client-version": "2026-05"},
                "auth_type": "bearer_token",
                "auth_header_name": "Authorization",
                "auth_header_prefix": "Bearer ",
                "tools": [
                    {
                        "name": "records.list",
                        "timeout_ms": 10000,
                        "enabled": True,
                    }
                ],
                "remove_tools": ["records.removed"],
            }
        },
    )


class McpServerResponse(BaseModel):
    id: str
    workspace_id: str
    scope_type: McpScopeType
    agent_id: str | None = None
    target_id: str | None = None
    target_type: McpRegistryTargetType | None = None
    target_constraints: AgentTargetConstraints = Field(default_factory=AgentTargetConstraints)
    server_name: str
    server_url: str
    enabled: bool
    auth_type: str
    credential_mode: Literal["none", "workspace", "individual"] = "none"
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    public_headers: dict[str, str] | None = None
    connection_status: Literal["unknown", "ok", "error"] = "unknown"
    last_discovery_at: datetime | None = None
    last_discovery_error: str | None = None
    catalog_source_id: str | None = None
    catalog_artifact_name: str | None = None
    catalog_version: str | None = None
    catalog_digest: str | None = None
    catalog_imported_at: datetime | None = None
    provenance_type: Literal["manual", "catalog", "builtin"] = "manual"
    endpoint_configuration: dict[str, Any] = Field(default_factory=dict)
    integration_profile_id: str | None = None
    integration_profile_version: int | None = None
    revision: int = 1
    tools: list[ToolConfigResponse]


class McpServerConnectionTestResponse(BaseModel):
    server_id: str
    server_name: str
    server_url: str
    connection_status: Literal["ok", "error"]
    last_discovery_at: datetime
    discovered_tool_count: int
    discovered_tools: list[str] = Field(default_factory=list)
    error: str | None = None


class McpConnectionUpsertRequest(BaseModel):
    workspace_id: str = Field(min_length=1)
    owner_type: Literal["installation", "user"]
    owner_id: str = Field(min_length=1)
    credential: str = Field(min_length=1, max_length=8192)
    consent_granted: Literal[True]

    @field_validator("credential")
    @classmethod
    def _validate_credential(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("credential must be no larger than 8 KiB")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValueError("credential must not contain control characters")
        return value

    model_config = ConfigDict(extra="forbid")


class McpConnectionResponse(BaseModel):
    server_id: str
    credential_mode: Literal["workspace", "individual"]
    status: Literal["missing", "connected", "error"]
    auth_type: Literal["bearer_token", "custom_header"]
    action: Literal["connect_mcp_server", "verify_mcp_server"] | None = None
    error_code: str | None = None
    verified_at: datetime | None = None
    updated_at: datetime | None = None


class McpConnectionVerifyRequest(BaseModel):
    workspace_id: str = Field(min_length=1)
    owner_type: Literal["installation", "user"]
    owner_id: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class McpPrincipalReference(BaseModel):
    type: Literal["user", "service_identity"]
    id: str = Field(min_length=1, max_length=256)

    model_config = ConfigDict(extra="forbid")


class McpExactToolReference(BaseModel):
    server_id: str = Field(min_length=1, max_length=64)
    tool_name: str = Field(min_length=1, max_length=256)

    model_config = ConfigDict(extra="forbid")


class McpReadinessRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=256)
    principal: McpPrincipalReference
    tool_refs: list[McpExactToolReference] = Field(max_length=200)

    model_config = ConfigDict(extra="forbid")


McpReadinessFailureCode = Literal[
    "MCP_INDIVIDUAL_USER_PRINCIPAL_REQUIRED",
    "MCP_CONNECTION_MISSING",
    "MCP_CONNECTION_ERROR",
    "MCP_CREDENTIAL_TOOL_UNAVAILABLE",
    "MCP_INSTALLATION_UNAVAILABLE",
    "MCP_REMOTE_DISABLED",
]


class McpReadinessFailure(BaseModel):
    server_id: str
    tool_name: str
    code: McpReadinessFailureCode
    action: Literal["connect_mcp_server", "verify_mcp_server"] | None = None


class McpReadinessResponse(BaseModel):
    ready: bool
    failures: list[McpReadinessFailure]
