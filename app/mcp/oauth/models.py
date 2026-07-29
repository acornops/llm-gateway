"""Validated internal models for MCP OAuth."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OAuthRegistrationMethod = Literal["cimd", "dcr"]


class OAuthEndpointSnapshot(BaseModel):
    """Pinned authorization-server endpoints used after discovery."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    authorization_response_iss_parameter_supported: bool = False

    model_config = ConfigDict(extra="forbid")


class OAuthIssuerCandidate(BaseModel):
    """Safe authorization-server information returned to the control plane."""

    issuer: str
    issuer_origin: str
    registration_method: OAuthRegistrationMethod
    scopes: list[str] = Field(default_factory=list)
    offline_access_requested: bool = False

    model_config = ConfigDict(extra="forbid")


class OAuthDiscoveryResult(BaseModel):
    """Validated result of one MCP OAuth discovery operation."""

    resource: str
    candidates: list[OAuthIssuerCandidate] = Field(min_length=1)
    endpoint_snapshots: dict[str, OAuthEndpointSnapshot]
    metadata_fingerprints: dict[str, str]

    model_config = ConfigDict(extra="forbid")


class OAuthPreparationRecord(BaseModel):
    """Short-lived preparation bound to an installation owner and browser."""

    workspace_id: str
    server_id: str
    owner_id: str
    browser_binding_hash: str
    return_path: str
    resource: str
    candidates: list[OAuthIssuerCandidate]
    endpoint_snapshots: dict[str, OAuthEndpointSnapshot]
    metadata_fingerprints: dict[str, str]

    model_config = ConfigDict(extra="forbid")


class OAuthFlowRecord(BaseModel):
    """Authorization-code flow state consumed exactly once by the callback."""

    workspace_id: str
    server_id: str
    owner_id: str
    browser_binding_hash: str
    return_path: str
    resource: str
    issuer: str
    client_id: str
    registration_method: OAuthRegistrationMethod
    scopes: list[str]
    code_verifier: str
    redirect_uri: str
    endpoint_snapshot: OAuthEndpointSnapshot
    metadata_fingerprint: str

    model_config = ConfigDict(extra="forbid")


class OAuthTokenBundle(BaseModel):
    """Versioned encrypted OAuth token material."""

    version: Literal[1] = 1
    access_token: str = Field(min_length=1, max_length=16384)
    refresh_token: str | None = Field(default=None, min_length=1, max_length=16384)
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    binding_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    model_config = ConfigDict(extra="forbid")
