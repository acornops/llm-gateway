from datetime import datetime
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from app.mcp.header_policy import validate_public_headers
from app.target_types import TargetType

ArtifactKind = Literal["mcp_server", "agent_skill"]


def normalize_registry_base_url(value: str) -> str:
    """Return a canonical HTTPS registry root without the adapter path."""
    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("registry base URL has an invalid host or port") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("registry base URL must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("registry base URL must not include credentials")
    if parsed.query:
        raise ValueError("registry base URL must not include query parameters")
    if parsed.fragment:
        raise ValueError("registry base URL must not include a fragment")
    path = parsed.path.rstrip("/")
    if path.endswith("/v0.1") or path == "v0.1":
        raise ValueError("registry base URL must not include /v0.1")
    return urlunparse(("https", parsed.netloc, path, "", "", ""))


class CatalogSourceCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=2048)
    auth_type: Literal["none", "bearer_token", "custom_header"] = "none"
    auth_secret_name: str | None = Field(default=None, max_length=240)
    auth_secret_value: str | None = Field(default=None, max_length=8192)
    auth_header_name: str | None = Field(default=None, max_length=120)
    network_route: Literal["direct", "connector"] = "direct"
    enabled: bool = True
    management_mode: Literal["workspace", "bootstrap"] = "workspace"
    artifact_kind: ArtifactKind = "mcp_server"
    adapter_type: Literal["mcp_registry_v0_1"] = "mcp_registry_v0_1"
    adapter_base_path: str = Field(default="/v0.1", min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_registry_base_url(value)

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if self.auth_type == "none" and any(
            (self.auth_secret_name, self.auth_secret_value, self.auth_header_name)
        ):
            raise ValueError("auth fields are not allowed when auth_type is none")
        if self.auth_type != "none" and not (
            self.auth_secret_name or self.auth_secret_value
        ):
            raise ValueError("authenticated sources require a secret reference or value")
        if self.auth_type == "custom_header" and not self.auth_header_name:
            raise ValueError("custom_header sources require auth_header_name")
        return self

    model_config = ConfigDict(extra="forbid")


class CatalogSourceAuthPatch(BaseModel):
    type: Literal["none", "bearer_token", "custom_header"]
    credential: str | None = Field(default=None, min_length=1, max_length=8192)
    header_name: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if self.type == "none" and (self.credential or self.header_name):
            raise ValueError("auth fields are not allowed when auth type is none")
        if self.type != "none" and not self.credential:
            raise ValueError("credential is required when replacing source authentication")
        if self.type == "custom_header" and not self.header_name:
            raise ValueError("header_name is required for custom_header authentication")
        if self.type != "custom_header" and self.header_name:
            raise ValueError("header_name is only allowed for custom_header authentication")
        return self

    model_config = ConfigDict(extra="forbid")


class CatalogSourcePatchRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None
    network_route: Literal["direct"] | None = None
    auth: CatalogSourceAuthPatch | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return normalize_registry_base_url(value) if value is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("catalog source patch must include at least one field")
        if "auth" in self.model_fields_set and self.auth is None:
            raise ValueError("auth must be an authentication object when provided")
        return self

    model_config = ConfigDict(extra="forbid")


class CatalogBindingResponse(BaseModel):
    id: str
    artifact_kind: ArtifactKind
    adapter_type: str
    adapter_base_path: str
    sync_status: Literal["pending", "syncing", "ready", "error"]
    last_sync_at: datetime | None = None
    last_sync_error: str | None = None


class CatalogSourceResponse(BaseModel):
    id: str
    workspace_id: str
    display_name: str
    base_url: str
    auth_type: str
    credential_configured: bool
    auth_header_name: str | None = None
    network_route: Literal["direct", "connector"]
    enabled: bool
    management_mode: Literal["workspace", "bootstrap"]
    bindings: list[CatalogBindingResponse]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CatalogSourceCapabilities(BaseModel):
    workspace_managed_sources_enabled: bool
    supported_network_routes: list[Literal["direct"]] = Field(
        default_factory=lambda: ["direct"]
    )


class CatalogSourceListResponse(BaseModel):
    items: list[CatalogSourceResponse]
    capabilities: CatalogSourceCapabilities


class CatalogArtifactResponse(BaseModel):
    id: str
    workspace_id: str
    source_id: str
    binding_id: str
    artifact_kind: ArtifactKind
    name: str
    title: str | None = None
    description: str
    version: str
    digest: str
    metadata: dict[str, Any]
    compatible: bool
    incompatibility_reason: str | None = None
    remote_endpoints: list[dict[str, Any]]
    published_at: datetime | None = None
    upstream_updated_at: datetime | None = None


class CatalogArtifactListResponse(BaseModel):
    items: list[CatalogArtifactResponse]
    next_cursor: str | None = None


class CatalogArtifactLocator(BaseModel):
    artifact_id: str | None = None
    source_id: str | None = None
    artifact_name: str | None = None

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        if self.artifact_id:
            return self
        if self.source_id and self.artifact_name:
            return self
        raise ValueError(
            "artifact locator requires artifact_id or source_id plus artifact_name"
        )


class CatalogMcpImportBase(BaseModel):
    workspace_id: str = Field(min_length=1)
    artifact: CatalogArtifactLocator
    version: str = Field(min_length=1, max_length=255)
    remote_endpoint: str = Field(min_length=1, max_length=2048)
    server_name: str | None = Field(default=None, min_length=1, max_length=160)
    public_headers: dict[str, str] | None = None
    endpoint_configuration: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    reimport_server_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("public_headers")
    @classmethod
    def validate_catalog_public_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_public_headers(value)

    @model_validator(mode="after")
    def validate_reimport(self) -> Self:
        if (self.reimport_server_id is None) != (self.expected_revision is None):
            raise ValueError(
                "reimport_server_id and expected_revision must be provided together"
            )
        return self

    model_config = ConfigDict(extra="forbid")


class CatalogAgentMcpImportRequest(CatalogMcpImportBase):
    scope_type: Literal["agent"] = "agent"
    agent_id: str = Field(min_length=1)
    target_constraints: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("target_constraints")
    @classmethod
    def validate_target_constraints(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        unknown = set(value) - {"target_types", "target_ids"}
        if unknown:
            raise ValueError("target constraints contain unsupported fields")
        return {
            key: sorted({item.strip() for item in items if item.strip()})
            for key, items in value.items()
        }


class CatalogTargetMcpImportRequest(CatalogMcpImportBase):
    scope_type: Literal["target"]
    target_id: str = Field(min_length=1)
    target_type: TargetType


CatalogMcpImportUnion = Annotated[
    CatalogAgentMcpImportRequest | CatalogTargetMcpImportRequest,
    Field(discriminator="scope_type"),
]


class CatalogMcpImportRequest(RootModel[CatalogMcpImportUnion]):
    """Discriminated import request with compatibility for legacy Agent payloads."""

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_agent_scope(cls, value: object) -> object:
        if isinstance(value, dict) and "scope_type" not in value and value.get("agent_id"):
            return {**value, "scope_type": "agent"}
        return value
