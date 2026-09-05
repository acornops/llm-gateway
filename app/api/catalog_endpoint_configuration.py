from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from fastapi import HTTPException

from app.api.mcp_admin_validation import validate_remote_mcp_endpoint_contract
from app.catalog.models import CatalogArtifact
from app.catalog.schemas import CatalogMcpImportBase
from app.mcp.egress_policy import McpEgressPolicyError, validate_mcp_server_url
from app.mcp.header_policy import (
    validate_public_auth_header_collision,
    validate_public_headers,
)

logger = structlog.get_logger()

_CREDENTIAL_MODES = ("none", "workspace", "individual")


@dataclass(frozen=True)
class ResolvedCatalogEndpoint:
    url: str
    public_headers: dict[str, str]
    supported_credential_modes: tuple[str, ...]
    credential_mode: str
    credential_header_name: str | None
    credential_auth_type: str
    credential_auth_header_prefix: str


def _supported_credential_modes(
    endpoint: dict[str, object],
    *,
    requires_credential: bool,
) -> tuple[str, ...]:
    inferred_modes = ("workspace", "individual") if requires_credential else ("none",)
    declared_modes = endpoint.get("supportedCredentialModes")
    if declared_modes is None:
        return inferred_modes
    if not isinstance(declared_modes, list) or not declared_modes:
        raise HTTPException(status_code=422, detail="Endpoint has no supported credential mode")
    if any(mode not in _CREDENTIAL_MODES for mode in declared_modes):
        raise HTTPException(
            status_code=422,
            detail="Endpoint declares an unsupported credential mode",
        )

    supported_modes = tuple(dict.fromkeys(declared_modes))
    if requires_credential:
        if "none" in supported_modes:
            raise HTTPException(
                status_code=422,
                detail="Credential-bearing endpoints cannot support credential mode none",
            )
    elif supported_modes != ("none",):
        raise HTTPException(
            status_code=422,
            detail="Credential-free endpoints must use credential mode none",
        )
    return supported_modes


async def resolve_catalog_endpoint(
    artifact: CatalogArtifact,
    request: CatalogMcpImportBase,
) -> ResolvedCatalogEndpoint:
    """Validate and resolve one secret-free endpoint from a pinned catalog artifact."""
    endpoint = next(
        (
            item
            for item in artifact.remote_endpoints or []
            if isinstance(item, dict) and item.get("url") == request.remote_endpoint
        ),
        None,
    )
    if endpoint is None:
        raise HTTPException(
            status_code=422,
            detail="Selected endpoint is not an installable endpoint for this artifact version",
        )
    if not artifact.compatible:
        raise HTTPException(status_code=422, detail=artifact.incompatibility_reason)
    if endpoint.get("supported") is False:
        raise HTTPException(
            status_code=422,
            detail=(
                "Selected endpoint requires an unsupported secret URL or "
                "multiple individual credentials"
            ),
        )

    configuration_fields = {
        field["name"]: field
        for field in endpoint.get("configurationFields") or []
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }
    configurable_names = {
        name
        for name, field in configuration_fields.items()
        if not field.get("secret") and "fixedValue" not in field
    }
    secret_configuration_names = {
        name for name, field in configuration_fields.items() if field.get("secret")
    }
    if set(request.endpoint_configuration) & secret_configuration_names:
        raise HTTPException(
            status_code=422,
            detail=(
                "Secret endpoint fields must be configured through the installation "
                "credential connection"
            ),
        )
    if set(request.endpoint_configuration) - configurable_names:
        raise HTTPException(
            status_code=422,
            detail="Endpoint configuration contains fields not declared by the artifact",
        )

    resolved_values: dict[str, str] = {}
    for name, field in configuration_fields.items():
        value = field.get("fixedValue")
        if not isinstance(value, str):
            value = request.endpoint_configuration.get(name)
        if not isinstance(value, str):
            value = field.get("default")
        if isinstance(value, str):
            resolved_values[name] = value
        elif field.get("required") and not field.get("secret"):
            raise HTTPException(
                status_code=422,
                detail=f"Endpoint configuration field {name} is required",
            )

    def substitute_template(value: str) -> str:
        return re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda match: resolved_values.get(match.group(1), match.group(0)),
            value,
        )

    resolved_url = substitute_template(request.remote_endpoint)
    if re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", resolved_url):
        raise HTTPException(
            status_code=422,
            detail="Endpoint URL contains unresolved configuration fields",
        )
    validate_remote_mcp_endpoint_contract(resolved_url)
    try:
        await validate_mcp_server_url(resolved_url)
    except McpEgressPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    configured_headers: dict[str, str] = {}
    for header in endpoint.get("publicHeaderTemplates") or []:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            continue
        template = header.get("value")
        if not isinstance(template, str):
            continue
        value = substitute_template(template)
        if re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", value):
            raise HTTPException(
                status_code=422,
                detail=f"Header {header['name']} contains unresolved configuration fields",
            )
        configured_headers[header["name"]] = value

    secret_header_names = {
        item for item in endpoint.get("secretHeaderNames") or [] if isinstance(item, str)
    }
    if len(secret_header_names) > 1:
        raise HTTPException(
            status_code=422,
            detail="Endpoint requires multiple credentials and is not supported",
        )
    credential_header_name = sorted(secret_header_names)[0] if secret_header_names else None
    credential_auth_type = (
        "none"
        if credential_header_name is None
        else "bearer_token"
        if credential_header_name.lower() == "authorization"
        else "custom_header"
    )
    supported_modes = _supported_credential_modes(
        endpoint,
        requires_credential=credential_header_name is not None,
    )
    recommended_mode = endpoint.get("recommendedCredentialMode")
    if recommended_mode is not None and recommended_mode not in supported_modes:
        raise HTTPException(
            status_code=422,
            detail="Endpoint recommends an unsupported credential mode",
        )
    credential_mode = request.credential_mode or (
        recommended_mode if isinstance(recommended_mode, str) else supported_modes[0]
    )
    if credential_mode not in supported_modes:
        raise HTTPException(
            status_code=422,
            detail="Selected credential mode is not supported by this endpoint",
        )
    merged_headers = {**(request.public_headers or {}), **configured_headers}
    try:
        validate_public_headers(merged_headers)
        validate_public_auth_header_collision(
            merged_headers,
            credential_auth_type,
            credential_header_name,
        )
    except ValueError as exc:
        logger.warning("catalog_public_header_rejected", reason=str(exc))
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_MCP_PUBLIC_HEADERS", "message": str(exc)},
        ) from exc
    return ResolvedCatalogEndpoint(
        url=resolved_url,
        public_headers=merged_headers,
        supported_credential_modes=supported_modes,
        credential_mode=credential_mode,
        credential_header_name=credential_header_name,
        credential_auth_type=credential_auth_type,
        credential_auth_header_prefix=(
            endpoint["credentialAuthHeaderPrefix"]
            if isinstance(endpoint.get("credentialAuthHeaderPrefix"), str)
            else "Bearer "
            if credential_auth_type == "bearer_token"
            else ""
        ),
    )
