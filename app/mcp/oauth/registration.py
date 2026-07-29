"""Public-client registration using CIMD or RFC 7591 DCR."""

from __future__ import annotations

import hashlib
import json

from app.config.settings import settings
from app.mcp.oauth.errors import oauth_error
from app.mcp.oauth.models import OAuthEndpointSnapshot, OAuthRegistrationMethod
from app.mcp.oauth.outbound import oauth_http_request


def callback_url() -> str:
    return (
        settings.MCP_OAUTH_PUBLIC_CONSOLE_URL.rstrip("/")
        + "/api/v1/mcp/oauth/callback"
    )


def client_metadata_url() -> str:
    return (
        settings.MCP_OAUTH_PUBLIC_CONSOLE_URL.rstrip("/")
        + "/api/v1/mcp/oauth/client-metadata"
    )


def public_client_metadata(scopes: list[str] | None = None) -> dict[str, object]:
    """Return the canonical public client metadata for CIMD and DCR."""

    metadata: dict[str, object] = {
        "client_id": client_metadata_url(),
        "client_name": "AcornOps",
        "client_uri": settings.MCP_OAUTH_PUBLIC_CONSOLE_URL.rstrip("/"),
        "redirect_uris": [callback_url()],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scopes:
        metadata["scope"] = " ".join(scopes)
    return metadata


def public_client_metadata_fingerprint(
    method: OAuthRegistrationMethod,
    scopes: list[str],
) -> str:
    """Bind reusable registrations to the exact public client configuration."""

    metadata = public_client_metadata(scopes if method == "dcr" else None)
    if method == "dcr":
        metadata.pop("client_id", None)
        metadata.update(
            {
                "application_type": "web",
                "software_id": "acornops",
                "software_version": "1",
            }
        )
    canonical = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def register_public_client(
    *,
    method: OAuthRegistrationMethod,
    endpoints: OAuthEndpointSnapshot,
    scopes: list[str],
) -> str:
    """Resolve the public client ID without ever accepting a client secret."""

    if method == "cimd":
        return client_metadata_url()
    if not endpoints.registration_endpoint:
        raise oauth_error(
            "MCP_OAUTH_AUTOMATIC_REGISTRATION_UNSUPPORTED",
            "The authorization server did not advertise dynamic registration.",
            status_code=409,
        )
    payload = public_client_metadata(scopes)
    payload.pop("client_id", None)
    payload.update(
        {
            "application_type": "web",
            "software_id": "acornops",
            "software_version": "1",
        }
    )
    response = await oauth_http_request(
        "POST",
        endpoints.registration_endpoint,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
        },
        json_body=payload,
    )
    if response.status_code in {429} or response.status_code >= 500:
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_UNAVAILABLE",
            "Dynamic client registration is temporarily unavailable.",
            status_code=503,
            retryable=True,
        )
    if response.status_code not in {200, 201}:
        raise oauth_error(
            "MCP_OAUTH_PUBLIC_REGISTRATION_REJECTED",
            "The authorization server rejected public dynamic client registration.",
            status_code=409,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_INVALID",
            "The authorization server returned an invalid registration response.",
        ) from exc
    if not isinstance(body, dict):
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_INVALID",
            "The authorization server returned an invalid registration response.",
        )
    client_id = body.get("client_id")
    auth_method = body.get("token_endpoint_auth_method")
    returned_redirects = body.get("redirect_uris")
    if (
        not isinstance(client_id, str)
        or not client_id
        or len(client_id) > 2048
        or any(not 0x20 <= ord(character) <= 0x7E for character in client_id)
    ):
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_INVALID",
            "The authorization server returned an invalid public client identifier.",
        )
    if auth_method != "none" or body.get("client_secret") is not None:
        raise oauth_error(
            "MCP_OAUTH_CONFIDENTIAL_CLIENT_UNSUPPORTED",
            "The authorization server created a confidential client instead of a public client.",
            status_code=409,
        )
    if returned_redirects != [callback_url()]:
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_REDIRECT_MISMATCH",
            "The authorization server did not register the exact OAuth callback URL.",
            status_code=409,
        )
    returned_grants = body.get("grant_types")
    if returned_grants is not None and (
        not isinstance(returned_grants, list)
        or any(not isinstance(value, str) for value in returned_grants)
        or "authorization_code" not in returned_grants
    ):
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_INVALID",
            "The authorization server registered an incompatible OAuth grant.",
            status_code=409,
        )
    returned_response_types = body.get("response_types")
    if returned_response_types is not None and (
        not isinstance(returned_response_types, list)
        or any(not isinstance(value, str) for value in returned_response_types)
        or "code" not in returned_response_types
    ):
        raise oauth_error(
            "MCP_OAUTH_REGISTRATION_INVALID",
            "The authorization server registered an incompatible OAuth response type.",
            status_code=409,
        )
    return client_id
