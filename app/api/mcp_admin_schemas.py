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

McpScopeType = Literal["workspace", "target"]
McpRegistryTargetType = TargetType | Literal["workspace"]


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

    @field_validator("name")
    @classmethod
    def _validate_tool_name(cls, value: str) -> str:
        tool_name = value.strip()
        if not tool_name:
            raise ValueError("tool name is required")
        if is_reserved_internal_tool_name(tool_name):
            raise ValueError("tool name is reserved by the platform")
        return tool_name


class ToolConfigResponse(BaseModel):
    name: str
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


class ToolUpdateRequest(BaseModel):
    enabled: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=120000)
    description: str | None = None
    capability: Literal["read", "write"] | None = None
    version: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    artifact_policy: Literal["never", "if_detailed", "always"] | None = None


class McpServerCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, examples=[EXAMPLE_WORKSPACE_ID])
    scope_type: McpScopeType = "target"
    target_id: str = Field(min_length=1)
    target_type: McpRegistryTargetType = Field(examples=TARGET_TYPE_EXAMPLES)
    server_name: str = Field(min_length=1, examples=["operations-catalog"])
    server_url: str = Field(min_length=1, examples=["https://mcp.example.com/v1/"])
    enabled: bool = True
    auth_type: Literal["none", "bearer_token", "custom_header"] = "none"
    auth_secret_name: str | None = None
    auth_secret_value: str | None = None
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    public_headers: dict[str, str] | None = None
    tools: list[ToolConfigRequest] = Field(default_factory=list)

    @field_validator("public_headers")
    @classmethod
    def _validate_public_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_public_headers(value)

    @field_validator("auth_header_name")
    @classmethod
    def _validate_auth_header_name(cls, value: str | None) -> str | None:
        return validate_auth_header_name(value)

    @field_validator("auth_header_prefix", "auth_secret_value")
    @classmethod
    def _validate_auth_header_value(cls, value: str | None) -> str | None:
        return validate_auth_header_value(value)

    @model_validator(mode="after")
    def _validate_auth_config(self) -> Self:
        if self.scope_type == "workspace" and (
            self.target_id != "__workspace__" or self.target_type != "workspace"
        ):
            raise ValueError(
                "workspace scope requires target_id=__workspace__ and target_type=workspace"
            )
        if self.scope_type == "target" and self.target_type == "workspace":
            raise ValueError("target scope requires a concrete target_type")
        if self.auth_secret_value is not None:
            validate_auth_header_value(
                f"{_effective_auth_header_prefix(self.auth_type, self.auth_header_prefix)}"
                f"{self.auth_secret_value}"
            )
        if self.auth_type == "none" and any(
            (
                self.auth_secret_name,
                self.auth_secret_value,
                self.auth_header_name,
                self.auth_header_prefix,
            )
        ):
            raise ValueError("auth fields are not allowed when auth_type is none")
        if self.auth_type in ("bearer_token", "custom_header") and not (
            self.auth_secret_name or self.auth_secret_value
        ):
            raise ValueError(
                "auth_secret_name or auth_secret_value is required for configured auth_type"
            )
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
                "auth_secret_name": "mcp_server::operations-catalog",
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
    server_name: str | None = None
    enabled: bool | None = None
    auth_type: Literal["none", "bearer_token", "custom_header"] | None = None
    auth_secret_name: str | None = None
    auth_secret_value: str | None = None
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    public_headers: dict[str, str] | None = None
    tools: list[ToolConfigRequest] | None = None
    remove_tools: list[str] = Field(default_factory=list)

    @field_validator("public_headers")
    @classmethod
    def _validate_public_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_public_headers(value)

    @field_validator("auth_header_name")
    @classmethod
    def _validate_auth_header_name(cls, value: str | None) -> str | None:
        return validate_auth_header_name(value)

    @field_validator("auth_header_prefix", "auth_secret_value")
    @classmethod
    def _validate_auth_header_value(cls, value: str | None) -> str | None:
        return validate_auth_header_value(value)

    @model_validator(mode="after")
    def _validate_constructed_auth_header_value(self) -> Self:
        if self.auth_secret_value is not None:
            validate_auth_header_value(
                f"{_effective_auth_header_prefix(self.auth_type, self.auth_header_prefix)}"
                f"{self.auth_secret_value}"
            )
        if self.auth_type == "none" and any(
            (
                self.auth_secret_name,
                self.auth_secret_value,
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
                "auth_secret_name": "mcp_server::operations-catalog",
                "auth_header_name": "Authorization",
                "auth_header_prefix": "Bearer ",
                "tools": [
                    {
                        "name": "records.list",
                        "timeout_ms": 10000,
                        "enabled": True,
                    }
                ],
                "remove_tools": ["records.deprecated"],
            }
        },
    )


class McpServerResponse(BaseModel):
    id: str
    workspace_id: str
    scope_type: McpScopeType
    target_id: str
    target_type: McpRegistryTargetType
    server_name: str
    server_url: str
    enabled: bool
    auth_type: str
    credential_configured: bool = False
    auth_header_name: str | None = None
    auth_header_prefix: str | None = None
    public_headers: dict[str, str] | None = None
    connection_status: Literal["unknown", "ok", "error"] = "unknown"
    last_discovery_at: datetime | None = None
    last_discovery_error: str | None = None
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
