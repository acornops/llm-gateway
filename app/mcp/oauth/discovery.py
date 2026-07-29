"""MCP protected-resource and authorization-server discovery."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
)
from mcp.client.streamable_http import MCP_PROTOCOL_VERSION
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import ValidationError

from app.config.settings import settings
from app.mcp.oauth.errors import oauth_error
from app.mcp.oauth.models import (
    OAuthDiscoveryResult,
    OAuthEndpointSnapshot,
    OAuthIssuerCandidate,
)
from app.mcp.oauth.outbound import (
    oauth_http_request,
    validate_oauth_endpoint_egress,
    validate_oauth_endpoint_url,
)
from app.mcp.oauth.scopes import normalize_oauth_scopes

MAX_OAUTH_AUTHORIZATION_SERVERS = 10


def _normalized_scopes(scope_text: str | None) -> list[str]:
    if not scope_text:
        return []
    return normalize_oauth_scopes(
        scope_text.split(),
        error_code="MCP_OAUTH_METADATA_INVALID",
        error_message="OAuth metadata contains invalid scopes.",
    )


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_issuer_url(issuer: str) -> None:
    """Apply the issuer-specific RFC 8414 URL constraints."""

    validate_oauth_endpoint_url(issuer)
    if urlparse(issuer).query:
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "Protected resource metadata contains an invalid authorization server.",
        )


def _resource_matches_server(server_url: str, resource: str) -> bool:
    """Validate an exact or path-parent resource without URI ambiguity."""

    if len(resource) > 4096:
        return False
    try:
        requested_resource = resource_url_from_server_url(server_url)
        requested = urlparse(requested_resource)
        configured = urlparse(resource)
        _ = requested.port
        _ = configured.port
    except ValueError:
        return False
    if (
        configured.scheme not in {"http", "https"}
        or not configured.hostname
        or configured.username is not None
        or configured.password is not None
        or configured.fragment
    ):
        return False
    # A query-bearing resource is a specific identifier, not a path parent.
    if configured.query and (
        configured.query != requested.query
        or configured.path != requested.path
    ):
        return False
    return check_resource_allowed(
        requested_resource=requested_resource,
        configured_resource=resource,
    )


def _parse_protected_resource_metadata(
    response: httpx.Response,
) -> ProtectedResourceMetadata | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    resource = payload.get("resource")
    if isinstance(resource, list):
        # A few remote implementations serialize the singular RFC 9728
        # resource identifier as a one-item array. Accept only the
        # unambiguous form; the normal resource-match check still applies.
        if len(resource) != 1 or not isinstance(resource[0], str):
            return None
        payload = {**payload, "resource": resource[0]}

    try:
        return ProtectedResourceMetadata.model_validate(payload)
    except ValidationError:
        return None


def _metadata_fingerprint(
    resource: str,
    metadata: OAuthMetadata,
    scopes: list[str],
    *,
    authorization_response_iss_parameter_supported: bool,
) -> str:
    payload = {
        "resource": resource,
        "issuer": str(metadata.issuer),
        "authorization_endpoint": str(metadata.authorization_endpoint),
        "token_endpoint": str(metadata.token_endpoint),
        "registration_endpoint": (
            str(metadata.registration_endpoint) if metadata.registration_endpoint else None
        ),
        "revocation_endpoint": (
            str(metadata.revocation_endpoint) if metadata.revocation_endpoint else None
        ),
        "client_id_metadata_document_supported": (
            metadata.client_id_metadata_document_supported is True
        ),
        "code_challenge_methods_supported": sorted(
            metadata.code_challenge_methods_supported or []
        ),
        "grant_types_supported": (
            sorted(metadata.grant_types_supported)
            if metadata.grant_types_supported is not None
            else None
        ),
        "response_types_supported": sorted(metadata.response_types_supported),
        "token_endpoint_auth_methods_supported": (
            sorted(metadata.token_endpoint_auth_methods_supported)
            if metadata.token_endpoint_auth_methods_supported is not None
            else None
        ),
        "authorization_response_iss_parameter_supported": (
            authorization_response_iss_parameter_supported
        ),
        "scopes": scopes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


async def _challenge(server_url: str) -> httpx.Response:
    body = {
        "jsonrpc": "2.0",
        "id": "oauth-discovery",
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "acornops-llm-gateway", "version": "1"},
        },
    }
    return await oauth_http_request(
        "POST",
        server_url,
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION,
        },
        json_body=body,
    )


async def _discover_protected_resource(
    server_url: str,
    challenge: httpx.Response,
) -> ProtectedResourceMetadata:
    challenge_url = (
        extract_resource_metadata_from_www_auth(challenge)
        if challenge.status_code == 401
        else None
    )
    urls = build_protected_resource_metadata_discovery_urls(challenge_url, server_url)
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        response = await oauth_http_request(
            "GET",
            url,
            headers={
                "accept": "application/json",
                MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION,
            },
        )
        if response.status_code == 429:
            raise oauth_error(
                "MCP_OAUTH_DISCOVERY_UNAVAILABLE",
                "OAuth discovery is temporarily unavailable.",
                status_code=503,
                retryable=True,
            )
        if response.status_code in {400, 401, 403, 404}:
            continue
        if response.status_code >= 500:
            raise oauth_error(
                "MCP_OAUTH_DISCOVERY_UNAVAILABLE",
                "OAuth discovery is temporarily unavailable.",
                status_code=503,
                retryable=True,
            )
        if response.status_code != 200:
            continue
        metadata = _parse_protected_resource_metadata(response)
        if metadata is None:
            continue
        if not _resource_matches_server(server_url, str(metadata.resource)):
            raise oauth_error(
                "MCP_OAUTH_RESOURCE_MISMATCH",
                "The protected resource metadata does not match this MCP server.",
            )
        return metadata
    raise oauth_error(
        "MCP_OAUTH_PROTECTED_RESOURCE_METADATA_MISSING",
        "The MCP server did not publish valid protected resource metadata.",
    )


async def _discover_authorization_server(
    issuer: str,
) -> tuple[OAuthMetadata, bool]:
    for url in build_oauth_authorization_server_metadata_discovery_urls(issuer, issuer):
        response = await oauth_http_request(
            "GET",
            url,
            headers={"accept": "application/json"},
        )
        if response.status_code == 429:
            raise oauth_error(
                "MCP_OAUTH_DISCOVERY_UNAVAILABLE",
                "Authorization-server discovery is temporarily unavailable.",
                status_code=503,
                retryable=True,
            )
        if response.status_code in {400, 401, 403, 404}:
            continue
        if response.status_code >= 500:
            raise oauth_error(
                "MCP_OAUTH_DISCOVERY_UNAVAILABLE",
                "Authorization-server discovery is temporarily unavailable.",
                status_code=503,
                retryable=True,
            )
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("response_types_supported"), list)
            ):
                continue
            metadata = OAuthMetadata.model_validate(payload)
        except (ValueError, ValidationError):
            continue
        if str(metadata.issuer) != issuer:
            raise oauth_error(
                "MCP_OAUTH_ISSUER_MISMATCH",
                "Authorization-server metadata returned a different issuer.",
            )
        iss_supported = payload.get(
            "authorization_response_iss_parameter_supported",
            False,
        )
        if not isinstance(iss_supported, bool):
            raise oauth_error(
                "MCP_OAUTH_METADATA_INVALID",
                "Authorization-server metadata contains an invalid issuer-response capability.",
            )
        return metadata, iss_supported
    raise oauth_error(
        "MCP_OAUTH_AUTHORIZATION_SERVER_METADATA_MISSING",
        "The authorization server did not publish valid metadata.",
    )


async def discover_mcp_oauth(server_url: str) -> OAuthDiscoveryResult:
    """Discover every advertised issuer and select automatic registration modes."""

    if not settings.MCP_OAUTH_ENABLED:
        raise oauth_error(
            "MCP_OAUTH_DISABLED",
            "MCP OAuth is disabled by platform policy.",
            status_code=409,
        )
    challenge = await _challenge(server_url)
    prm = await _discover_protected_resource(server_url, challenge)
    challenge_scope = (
        extract_scope_from_www_auth(challenge) if challenge.status_code == 401 else None
    )
    base_scopes = _normalized_scopes(challenge_scope)
    if not base_scopes:
        base_scopes = normalize_oauth_scopes(
            prm.scopes_supported or [],
            error_code="MCP_OAUTH_METADATA_INVALID",
            error_message="Protected resource metadata contains invalid scopes.",
        )
    # `offline_access` is a client-selected refresh capability, not a
    # resource API permission.  A protected resource must not be able to force
    # it through either its challenge or protected-resource metadata.  Add it
    # per issuer below only when that authorization server advertises support,
    # so the preparation response always discloses the request.
    base_scopes = [scope for scope in base_scopes if scope != "offline_access"]

    advertised_issuers = list(dict.fromkeys(str(value) for value in prm.authorization_servers))
    if (
        not advertised_issuers
        or len(advertised_issuers) > MAX_OAUTH_AUTHORIZATION_SERVERS
    ):
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "Protected resource metadata contains an invalid authorization-server list.",
        )

    candidates: list[OAuthIssuerCandidate] = []
    snapshots: dict[str, OAuthEndpointSnapshot] = {}
    fingerprints: dict[str, str] = {}
    for issuer in advertised_issuers:
        if len(issuer) > 2048:
            raise oauth_error(
                "MCP_OAUTH_METADATA_INVALID",
                "Protected resource metadata contains an invalid authorization server.",
            )
        _validate_issuer_url(issuer)
        metadata, authorization_response_iss_parameter_supported = (
            await _discover_authorization_server(issuer)
        )
        if "S256" not in (metadata.code_challenge_methods_supported or []):
            raise oauth_error(
                "MCP_OAUTH_PKCE_S256_UNSUPPORTED",
                "The authorization server does not advertise PKCE S256 support.",
                status_code=409,
            )
        if (
            metadata.grant_types_supported is not None
            and "authorization_code" not in metadata.grant_types_supported
        ):
            raise oauth_error(
                "MCP_OAUTH_AUTHORIZATION_CODE_UNSUPPORTED",
                "The authorization server does not advertise the authorization-code grant.",
                status_code=409,
            )
        if "code" not in metadata.response_types_supported:
            raise oauth_error(
                "MCP_OAUTH_CODE_RESPONSE_UNSUPPORTED",
                "The authorization server does not advertise the code response type.",
                status_code=409,
            )
        if metadata.client_id_metadata_document_supported is True:
            method = "cimd"
        elif metadata.registration_endpoint is not None:
            method = "dcr"
        else:
            continue
        endpoints = [
            str(metadata.authorization_endpoint),
            str(metadata.token_endpoint),
        ]
        if metadata.revocation_endpoint is not None:
            endpoints.append(str(metadata.revocation_endpoint))
        if method == "dcr" and metadata.registration_endpoint is not None:
            endpoints.append(str(metadata.registration_endpoint))
        for endpoint in endpoints:
            await validate_oauth_endpoint_egress(endpoint)
        if (
            method == "cimd"
            and "none" not in (metadata.token_endpoint_auth_methods_supported or [])
        ):
            raise oauth_error(
                "MCP_OAUTH_PUBLIC_CLIENT_UNSUPPORTED",
                "The authorization server does not advertise public-client token authentication.",
                status_code=409,
            )

        advertised_scopes = normalize_oauth_scopes(
            metadata.scopes_supported or [],
            error_code="MCP_OAUTH_METADATA_INVALID",
            error_message="Authorization-server metadata contains invalid scopes.",
        )
        scopes = list(base_scopes)
        offline_access_requested = "offline_access" in advertised_scopes
        if offline_access_requested:
            scopes.append("offline_access")
            scopes.sort()
        snapshot = OAuthEndpointSnapshot(
            issuer=str(metadata.issuer),
            authorization_endpoint=str(metadata.authorization_endpoint),
            token_endpoint=str(metadata.token_endpoint),
            registration_endpoint=(
                str(metadata.registration_endpoint)
                if method == "dcr" and metadata.registration_endpoint is not None
                else None
            ),
            revocation_endpoint=(
                str(metadata.revocation_endpoint)
                if metadata.revocation_endpoint is not None
                else None
            ),
            authorization_response_iss_parameter_supported=(
                authorization_response_iss_parameter_supported
            ),
        )
        candidates.append(
            OAuthIssuerCandidate(
                issuer=issuer,
                issuer_origin=_origin(issuer),
                registration_method=method,
                scopes=scopes,
                offline_access_requested=offline_access_requested,
            )
        )
        snapshots[issuer] = snapshot
        fingerprints[issuer] = _metadata_fingerprint(
            str(prm.resource),
            metadata,
            scopes,
            authorization_response_iss_parameter_supported=(
                authorization_response_iss_parameter_supported
            ),
        )

    if not candidates:
        raise oauth_error(
            "MCP_OAUTH_AUTOMATIC_REGISTRATION_UNSUPPORTED",
            (
                "The authorization server does not support CIMD or public dynamic "
                "client registration."
            ),
            status_code=409,
        )
    candidates.sort(key=lambda candidate: candidate.issuer)
    return OAuthDiscoveryResult(
        resource=str(prm.resource),
        candidates=candidates,
        endpoint_snapshots=snapshots,
        metadata_fingerprints=fingerprints,
    )
