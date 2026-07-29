"""Application service for MCP OAuth preparation, start, and completion."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.client.auth.oauth2 import PKCEParameters

from app.mcp.connections import ConnectionOwner, mcp_connection_store
from app.mcp.oauth.discovery import discover_mcp_oauth
from app.mcp.oauth.errors import McpOAuthError, oauth_error
from app.mcp.oauth.flow_store import oauth_flow_store
from app.mcp.oauth.models import (
    OAuthFlowRecord,
    OAuthPreparationRecord,
    OAuthTokenBundle,
)
from app.mcp.oauth.registration import (
    callback_url,
    public_client_metadata_fingerprint,
    register_public_client,
)
from app.mcp.oauth.registration_store import oauth_registration_store
from app.mcp.oauth.tokens import oauth_token_service


def _require_browser_binding(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise oauth_error(
            "MCP_OAUTH_BROWSER_BINDING_REQUIRED",
            "The OAuth browser binding is missing or invalid.",
            status_code=400,
        )
    return value


def _require_safe_return_path(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or len(value) > 2048
    ):
        raise oauth_error(
            "MCP_OAUTH_RETURN_PATH_INVALID",
            "The OAuth return path is invalid.",
            status_code=400,
        )
    return value


def _authorization_url(endpoint: str, params: dict[str, str]) -> str:
    """Merge provider-fixed query fields without allowing OAuth parameter collisions."""

    try:
        parsed = urlsplit(endpoint)
        existing = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=50,
        )
    except ValueError as exc:
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "The authorization server advertised an invalid authorization endpoint.",
        ) from exc
    if any(name in params for name, _value in existing):
        raise oauth_error(
            "MCP_OAUTH_METADATA_INVALID",
            "The authorization endpoint contains conflicting OAuth parameters.",
        )
    query = urlencode([*existing, *params.items()])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _registration_matches_client_metadata(
    registration,
    *,
    method: str,
    fingerprint: str,
) -> bool:
    """Only reuse a client created for the current redirect and public metadata."""

    return bool(
        registration is not None
        and registration.registration_method == method
        and registration.client_metadata_fingerprint == fingerprint
    )


async def prepare_authorization(
    *,
    server,
    workspace_id: str,
    owner_id: str,
    browser_binding_hash: str,
    return_path: str,
) -> tuple[str, OAuthPreparationRecord]:
    """Discover provider capabilities and persist a short-lived preparation."""

    if server.auth_type != "oauth" or server.credential_mode != "individual":
        raise oauth_error(
            "MCP_OAUTH_INSTALLATION_REQUIRED",
            "This MCP installation does not use individual OAuth.",
            status_code=409,
        )
    validated_binding_hash = _require_browser_binding(browser_binding_hash)
    validated_return_path = _require_safe_return_path(return_path)
    server_id = str(server.id)
    owner = ConnectionOwner("user", owner_id)
    async with mcp_connection_store.mutation_lock(
        workspace_id,
        server_id,
        owner,
    ):
        existing = await mcp_connection_store.get(workspace_id, server_id, owner)
        if existing is None:
            existing = await mcp_connection_store.upsert(
                workspace_id=workspace_id,
                server_id=server_id,
                owner=owner,
                status="pending_authorization",
                error_code=None,
            )
        if existing is None:
            raise oauth_error(
                "MCP_OAUTH_INSTALLATION_NOT_FOUND",
                "The MCP installation no longer exists.",
                status_code=404,
            )
        connection_id = str(existing.id)

    discovery = await discover_mcp_oauth(server.server_url)
    record = OAuthPreparationRecord(
        workspace_id=workspace_id,
        server_id=server_id,
        owner_id=owner_id,
        browser_binding_hash=validated_binding_hash,
        return_path=validated_return_path,
        resource=discovery.resource,
        candidates=discovery.candidates,
        endpoint_snapshots=discovery.endpoint_snapshots,
        metadata_fingerprints=discovery.metadata_fingerprints,
    )
    async with mcp_connection_store.mutation_lock(
        workspace_id,
        server_id,
        owner,
    ):
        current = await mcp_connection_store.get(workspace_id, server_id, owner)
        if current is None or str(current.id) != connection_id:
            raise oauth_error(
                "MCP_OAUTH_INSTALLATION_NOT_FOUND",
                "The MCP installation no longer exists.",
                status_code=404,
            )
        handle = await oauth_flow_store.create_preparation(record)
    return handle, record


async def start_authorization(
    *,
    preparation_handle: str,
    workspace_id: str,
    server_id: str,
    owner_id: str,
    browser_binding_hash: str,
    issuer: str | None,
    consent_granted: bool,
) -> tuple[str, str, bool]:
    """Resolve a public client and create a callback-bound authorization URL."""

    if not consent_granted:
        raise oauth_error(
            "MCP_OAUTH_CONSENT_REQUIRED",
            "Explicit consent is required before authorization starts.",
            status_code=400,
        )
    preparation = await oauth_flow_store.consume_preparation(preparation_handle)
    supplied_binding_hash = _require_browser_binding(browser_binding_hash)
    if (
        preparation.workspace_id != workspace_id
        or preparation.server_id != server_id
        or preparation.owner_id != owner_id
        or not secrets.compare_digest(
            preparation.browser_binding_hash,
            supplied_binding_hash,
        )
    ):
        raise oauth_error(
            "MCP_OAUTH_FLOW_BINDING_MISMATCH",
            "The OAuth request does not match the initiating connection.",
            status_code=400,
        )
    candidate_by_issuer = {
        candidate.issuer: candidate for candidate in preparation.candidates
    }
    selected_issuer = issuer
    if selected_issuer is None and len(candidate_by_issuer) == 1:
        selected_issuer = next(iter(candidate_by_issuer))
    if selected_issuer not in candidate_by_issuer:
        raise oauth_error(
            "MCP_OAUTH_ISSUER_SELECTION_REQUIRED",
            "Select one of the authorization servers advertised by the MCP server.",
            status_code=409,
        )
    candidate = candidate_by_issuer[selected_issuer]
    endpoints = preparation.endpoint_snapshots[selected_issuer]
    fingerprint = preparation.metadata_fingerprints[selected_issuer]
    scopes = candidate.scopes
    owner = ConnectionOwner("user", owner_id)
    async with mcp_connection_store.mutation_lock(
        workspace_id,
        server_id,
        owner,
    ):
        connection = await mcp_connection_store.get(
            workspace_id,
            server_id,
            owner,
        )
        if connection is None:
            raise oauth_error(
                "MCP_OAUTH_FLOW_INVALID",
                "The OAuth request is invalid or expired.",
                status_code=400,
            )
        client_metadata_fingerprint = public_client_metadata_fingerprint(
            candidate.registration_method,
            scopes,
        )

        async with oauth_registration_store.registration_lock(
            workspace_id,
            server_id,
            selected_issuer,
        ):
            registration = await oauth_registration_store.get(
                workspace_id,
                server_id,
                selected_issuer,
            )
            metadata_changed = bool(
                registration is not None
                and (
                    registration.metadata_fingerprint != fingerprint
                    or registration.client_metadata_fingerprint
                    != client_metadata_fingerprint
                )
            )
            if not _registration_matches_client_metadata(
                registration,
                method=candidate.registration_method,
                fingerprint=client_metadata_fingerprint,
            ):
                registration = None
            client_id = (
                registration.client_id
                if registration is not None
                else await register_public_client(
                    method=candidate.registration_method,
                    endpoints=endpoints,
                    scopes=scopes,
                )
            )
            persisted = await oauth_registration_store.put(
                workspace_id=workspace_id,
                server_id=server_id,
                resource=preparation.resource,
                issuer=selected_issuer,
                registration_method=candidate.registration_method,
                client_id=client_id,
                endpoint_snapshot=endpoints,
                metadata_fingerprint=fingerprint,
                client_metadata_fingerprint=client_metadata_fingerprint,
                scopes=scopes,
            )
            if persisted is None:
                raise oauth_error(
                    "MCP_OAUTH_INSTALLATION_NOT_FOUND",
                    "The MCP installation no longer exists.",
                    status_code=404,
                )

        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        redirect_uri = callback_url()
        flow = OAuthFlowRecord(
            workspace_id=workspace_id,
            server_id=server_id,
            owner_id=owner_id,
            browser_binding_hash=preparation.browser_binding_hash,
            return_path=preparation.return_path,
            resource=preparation.resource,
            issuer=selected_issuer,
            client_id=client_id,
            registration_method=candidate.registration_method,
            scopes=scopes,
            code_verifier=pkce.code_verifier,
            redirect_uri=redirect_uri,
            endpoint_snapshot=endpoints,
            metadata_fingerprint=fingerprint,
        )
        await oauth_flow_store.create_flow(state, flow)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            "resource": preparation.resource,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        authorization_url = _authorization_url(
            endpoints.authorization_endpoint,
            params,
        )
        updated = await mcp_connection_store.set_state(
            connection,
            "pending_authorization",
            oauth_issuer=selected_issuer,
            oauth_registration_method=candidate.registration_method,
            oauth_resource=preparation.resource,
            oauth_scopes=scopes,
        )
        if updated is None:
            await oauth_flow_store.delete_for_connection(
                workspace_id,
                server_id,
                owner_id,
            )
            raise oauth_error(
                "MCP_OAUTH_INSTALLATION_NOT_FOUND",
                "The MCP installation no longer exists.",
                status_code=404,
            )
        return authorization_url, state, metadata_changed


async def complete_authorization(
    *,
    code: str | None,
    state: str,
    issuer: str | None,
    provider_error: str | None,
    owner_id: str,
    browser_binding_hash: str,
    verify_connection: Callable[
        [OAuthFlowRecord, OAuthTokenBundle, object],
        Awaitable[object],
    ],
):
    """Consume callback state, persist tokens, and verify under one owner lock."""

    flow = await oauth_flow_store.consume_flow(state)
    supplied_binding_hash = _require_browser_binding(browser_binding_hash)
    if flow.owner_id != owner_id or not secrets.compare_digest(
        flow.browser_binding_hash,
        supplied_binding_hash,
    ):
        raise oauth_error(
            "MCP_OAUTH_FLOW_BINDING_MISMATCH",
            "The OAuth callback does not match the initiating browser.",
            status_code=400,
        )
    owner = ConnectionOwner("user", flow.owner_id)
    async with mcp_connection_store.mutation_lock(
        flow.workspace_id,
        flow.server_id,
        owner,
    ):
        connection = await mcp_connection_store.get(
            flow.workspace_id,
            flow.server_id,
            owner,
        )
        if (
            connection is None
            or connection.status != "pending_authorization"
            or connection.oauth_issuer != flow.issuer
        ):
            raise oauth_error(
                "MCP_OAUTH_FLOW_INVALID",
                "The OAuth request is invalid or expired.",
                status_code=400,
                return_path=flow.return_path,
                workspace_id=flow.workspace_id,
                server_id=flow.server_id,
            )
        issuer_required = (
            flow.endpoint_snapshot.authorization_response_iss_parameter_supported
        )
        if (issuer_required and issuer is None) or (
            issuer is not None and issuer != flow.issuer
        ):
            await mcp_connection_store.set_state(
                connection,
                "pending_authorization",
                error_code="MCP_OAUTH_ISSUER_MISMATCH",
            )
            raise oauth_error(
                "MCP_OAUTH_ISSUER_MISMATCH",
                "The authorization callback returned a different issuer.",
                status_code=400,
                return_path=flow.return_path,
                workspace_id=flow.workspace_id,
                server_id=flow.server_id,
            )
        if provider_error:
            await mcp_connection_store.set_state(
                connection,
                "pending_authorization",
                error_code="MCP_OAUTH_AUTHORIZATION_DENIED",
            )
            raise oauth_error(
                "MCP_OAUTH_AUTHORIZATION_DENIED",
                "Authorization was denied or cancelled.",
                status_code=409,
                return_path=flow.return_path,
                workspace_id=flow.workspace_id,
                server_id=flow.server_id,
            )
        if not code or len(code) > 8192:
            raise oauth_error(
                "MCP_OAUTH_CALLBACK_INVALID",
                "The authorization callback is invalid.",
                status_code=400,
                return_path=flow.return_path,
                workspace_id=flow.workspace_id,
                server_id=flow.server_id,
            )
        try:
            bundle = await oauth_token_service.exchange_authorization_code(
                workspace_id=flow.workspace_id,
                server_id=flow.server_id,
                owner_id=flow.owner_id,
                client_id=flow.client_id,
                code=code,
                code_verifier=flow.code_verifier,
                redirect_uri=flow.redirect_uri,
                resource=flow.resource,
                scopes=flow.scopes,
                endpoint_snapshot=flow.endpoint_snapshot,
            )
        except McpOAuthError as error:
            error.return_path = flow.return_path
            error.workspace_id = flow.workspace_id
            error.server_id = flow.server_id
            raise
        try:
            connection = await mcp_connection_store.set_state(
                connection,
                "pending_authorization",
                oauth_issuer=flow.issuer,
                oauth_registration_method=flow.registration_method,
                oauth_resource=flow.resource,
                oauth_client_id=flow.client_id,
                oauth_endpoint_snapshot=flow.endpoint_snapshot.model_dump(),
                oauth_scopes=bundle.scopes,
                oauth_token_expires_at=bundle.expires_at,
                oauth_refresh_capable=bool(bundle.refresh_token),
            )
        except Exception as exc:
            await oauth_token_service.best_effort_revoke_bundle(
                endpoint_snapshot=flow.endpoint_snapshot,
                client_id=flow.client_id,
                bundle=bundle,
            )
            await oauth_token_service.delete_tokens(
                flow.workspace_id,
                flow.server_id,
                flow.owner_id,
            )
            with suppress(Exception):
                await mcp_connection_store.set_state(
                    connection,
                    "reauthorization_required",
                    error_code="MCP_OAUTH_TOKEN_PERSISTENCE_FAILED",
                )
            raise oauth_error(
                "MCP_OAUTH_TOKEN_PERSISTENCE_FAILED",
                "Authorization completed, but its connection state could not be stored securely.",
                status_code=503,
                return_path=flow.return_path,
                workspace_id=flow.workspace_id,
                server_id=flow.server_id,
            ) from exc
        if connection is None:
            await oauth_token_service.best_effort_revoke_bundle(
                endpoint_snapshot=flow.endpoint_snapshot,
                client_id=flow.client_id,
                bundle=bundle,
            )
            await oauth_token_service.delete_tokens(
                flow.workspace_id,
                flow.server_id,
                flow.owner_id,
            )
            raise oauth_error(
                "MCP_OAUTH_INSTALLATION_NOT_FOUND",
                "The MCP installation no longer exists.",
                status_code=404,
            )
        connection = await verify_connection(flow, bundle, connection)
        return flow, bundle, connection
