"""Opaque OAuth token exchange, encrypted storage, refresh, and revocation."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

from app.config.settings import settings
from app.mcp.connections import ConnectionOwner, mcp_connection_store
from app.mcp.header_policy import MAX_HEADER_VALUE_LENGTH
from app.mcp.oauth.errors import McpOAuthError, oauth_error
from app.mcp.oauth.models import OAuthEndpointSnapshot, OAuthTokenBundle
from app.mcp.oauth.outbound import oauth_http_request
from app.mcp.oauth.scopes import normalize_oauth_scopes
from app.mcp.oauth.token_binding import (
    connection_token_binding,
    token_binding_fingerprint,
)
from app.secrets.errors import SecretNotFoundError
from app.secrets.store import secret_store


def oauth_token_secret_name(
    workspace_id: str,
    server_id: str,
    owner_id: str,
) -> str:
    if not owner_id:
        raise ValueError("OAuth token owner is required")
    return f"mcp_oauth_tokens::{workspace_id}::{server_id}::user::{owner_id}"


def _token_scope(workspace_id: str) -> dict[str, str]:
    return {"workspace_id": workspace_id}


def _valid_opaque_token(value: object, *, max_length: int) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= max_length
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _parse_token_response(
    payload: object,
    *,
    requested_scopes: list[str],
    previous_refresh_token: str | None = None,
) -> OAuthTokenBundle:
    if not isinstance(payload, dict):
        raise oauth_error(
            "MCP_OAUTH_TOKEN_RESPONSE_INVALID",
            "The authorization server returned an invalid token response.",
        )
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    refresh_token = payload.get("refresh_token", previous_refresh_token)
    expires_in = payload.get("expires_in")
    if (
        not _valid_opaque_token(
            access_token,
            max_length=MAX_HEADER_VALUE_LENGTH - len("Bearer "),
        )
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or (
            refresh_token is not None
            and not _valid_opaque_token(refresh_token, max_length=16384)
        )
    ):
        raise oauth_error(
            "MCP_OAUTH_TOKEN_RESPONSE_INVALID",
            "The authorization server returned an invalid token response.",
        )
    expires_at = None
    if expires_in is not None:
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in < 0
            or expires_in > 315_576_000
        ):
            raise oauth_error(
                "MCP_OAUTH_TOKEN_RESPONSE_INVALID",
                "The authorization server returned an invalid token lifetime.",
            )
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    raw_scope = payload.get("scope")
    if raw_scope is not None and not isinstance(raw_scope, str):
        raise oauth_error(
            "MCP_OAUTH_TOKEN_RESPONSE_INVALID",
            "The authorization server returned invalid token scopes.",
        )
    scopes = normalize_oauth_scopes(
        raw_scope.split() if isinstance(raw_scope, str) else requested_scopes,
        error_code="MCP_OAUTH_TOKEN_RESPONSE_INVALID",
        error_message="The authorization server returned invalid token scopes.",
    )
    return OAuthTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        expires_at=expires_at,
        scopes=scopes,
    )


class OAuthTokenService:
    """Own token mutation and refresh serialization."""

    async def best_effort_revoke_bundle(
        self,
        *,
        endpoint_snapshot: OAuthEndpointSnapshot,
        client_id: str,
        bundle: OAuthTokenBundle,
    ) -> None:
        """Limit exposure when issued tokens cannot be retained safely."""

        if not endpoint_snapshot.revocation_endpoint:
            return
        tokens = [
            (bundle.refresh_token, "refresh_token"),
            (bundle.access_token, "access_token"),
        ]
        seen: set[str] = set()
        for token, token_type_hint in tokens:
            if not token or token in seen:
                continue
            seen.add(token)
            with suppress(Exception):
                await oauth_http_request(
                    "POST",
                    endpoint_snapshot.revocation_endpoint,
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    form_body={
                        "token": token,
                        "token_type_hint": token_type_hint,
                        "client_id": client_id,
                    },
                )

    @asynccontextmanager
    async def _connection_lock(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        *,
        already_held: bool,
    ) -> AsyncIterator[None]:
        if already_held:
            yield
            return
        async with mcp_connection_store.mutation_lock(
            workspace_id,
            server_id,
            ConnectionOwner("user", owner_id),
        ):
            yield

    async def _load(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
    ) -> OAuthTokenBundle:
        value = await secret_store.get_secret(
            oauth_token_secret_name(workspace_id, server_id, owner_id),
            _token_scope(workspace_id),
        )
        try:
            bundle = OAuthTokenBundle.model_validate_json(value)
            if (
                not _valid_opaque_token(
                    bundle.access_token,
                    max_length=MAX_HEADER_VALUE_LENGTH - len("Bearer "),
                )
                or (
                    bundle.refresh_token is not None
                    and not _valid_opaque_token(
                        bundle.refresh_token,
                        max_length=16384,
                    )
                )
                or (
                    bundle.expires_at is not None
                    and bundle.expires_at.utcoffset() is None
                )
                or bundle.binding_fingerprint is None
            ):
                raise ValueError("stored OAuth token bundle is invalid")
            normalize_oauth_scopes(
                bundle.scopes,
                error_code="MCP_OAUTH_TOKEN_SECRET_INVALID",
                error_message="The stored OAuth connection must be authorized again.",
            )
            return bundle
        except Exception as exc:
            raise oauth_error(
                "MCP_OAUTH_TOKEN_SECRET_INVALID",
                "The stored OAuth connection must be authorized again.",
                status_code=409,
            ) from exc

    async def _save(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        bundle: OAuthTokenBundle,
    ) -> None:
        await secret_store.put_secret(
            oauth_token_secret_name(workspace_id, server_id, owner_id),
            bundle.model_dump_json(),
            _token_scope(workspace_id),
        )

    async def access_token_matches(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        expected_fingerprint: str,
    ) -> bool:
        """Check a failed request against the currently stored opaque token."""

        try:
            bundle = await self._load(workspace_id, server_id, owner_id)
        except (SecretNotFoundError, McpOAuthError):
            return False
        actual = hashlib.sha256(bundle.access_token.encode()).hexdigest()
        return secrets.compare_digest(actual, expected_fingerprint)

    async def _reauthorization_required(
        self,
        connection,
        *,
        error_code: str,
        cause: Exception,
    ) -> None:
        await mcp_connection_store.set_state(
            connection,
            "reauthorization_required",
            error_code=error_code,
        )
        raise oauth_error(
            "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
            "Authorize this MCP server again.",
            status_code=409,
        ) from cause

    async def _load_for_access(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        connection,
    ) -> OAuthTokenBundle:
        try:
            return await self._load(workspace_id, server_id, owner_id)
        except SecretNotFoundError as exc:
            await self._reauthorization_required(
                connection,
                error_code="MCP_OAUTH_TOKEN_SECRET_MISSING",
                cause=exc,
            )
        except McpOAuthError as exc:
            await self._reauthorization_required(
                connection,
                error_code="MCP_OAUTH_TOKEN_SECRET_INVALID",
                cause=exc,
            )
        raise AssertionError("reauthorization transition must raise")

    async def exchange_authorization_code(
        self,
        *,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        client_id: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        resource: str,
        scopes: list[str],
        endpoint_snapshot: OAuthEndpointSnapshot,
    ) -> OAuthTokenBundle:
        response = await oauth_http_request(
            "POST",
            endpoint_snapshot.token_endpoint,
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            },
            form_body={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
                "resource": resource,
            },
        )
        if response.status_code in {429} or response.status_code >= 500:
            raise oauth_error(
                "MCP_OAUTH_TOKEN_ENDPOINT_UNAVAILABLE",
                "The authorization server is temporarily unavailable.",
                status_code=503,
                retryable=True,
            )
        if response.status_code != 200:
            raise oauth_error(
                "MCP_OAUTH_CODE_EXCHANGE_FAILED",
                "Authorization could not be completed.",
                status_code=409,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise oauth_error(
                "MCP_OAUTH_TOKEN_RESPONSE_INVALID",
                "The authorization server returned an invalid token response.",
            ) from exc
        bundle = _parse_token_response(payload, requested_scopes=scopes)
        bundle = bundle.model_copy(
            update={
                "binding_fingerprint": token_binding_fingerprint(
                    endpoint_snapshot=endpoint_snapshot,
                    client_id=client_id,
                    resource=resource,
                )
            }
        )
        try:
            await self._save(workspace_id, server_id, owner_id, bundle)
        except Exception as exc:
            await self.best_effort_revoke_bundle(
                endpoint_snapshot=endpoint_snapshot,
                client_id=client_id,
                bundle=bundle,
            )
            with suppress(Exception):
                await self.delete_tokens(workspace_id, server_id, owner_id)
            with suppress(Exception):
                connection = await mcp_connection_store.get(
                    workspace_id,
                    server_id,
                    ConnectionOwner("user", owner_id),
                )
                if connection is not None:
                    await mcp_connection_store.set_state(
                        connection,
                        "reauthorization_required",
                        error_code="MCP_OAUTH_TOKEN_PERSISTENCE_FAILED",
                    )
            raise oauth_error(
                "MCP_OAUTH_TOKEN_PERSISTENCE_FAILED",
                "Authorization completed, but the tokens could not be stored securely.",
                status_code=503,
            ) from exc
        return bundle

    async def access_token(
        self,
        *,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        connection,
        mutation_lock_held: bool = False,
    ) -> str:
        """Return a usable token, refreshing once before dispatch when needed."""

        try:
            bundle = await self._load(workspace_id, server_id, owner_id)
        except (SecretNotFoundError, McpOAuthError):
            bundle = None
        safety = timedelta(seconds=settings.MCP_OAUTH_REFRESH_SAFETY_SECONDS)
        initial_binding = connection_token_binding(connection)
        if (
            bundle is not None
            and initial_binding is not None
            and bundle.binding_fingerprint == initial_binding[3]
            and (
                bundle.expires_at is None
                or bundle.expires_at > datetime.now(UTC) + safety
            )
        ):
            return bundle.access_token

        async with self._connection_lock(
            workspace_id,
            server_id,
            owner_id,
            already_held=mutation_lock_held,
        ):
            current = await mcp_connection_store.get(
                workspace_id,
                server_id,
                ConnectionOwner("user", owner_id),
            )
            if current is None:
                raise oauth_error(
                    "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
                    "Authorize this MCP server again.",
                    status_code=409,
                )
            connection = current
            bundle = await self._load_for_access(
                workspace_id,
                server_id,
                owner_id,
                connection,
            )
            binding = connection_token_binding(connection)
            if binding is None or bundle.binding_fingerprint != binding[3]:
                await mcp_connection_store.set_state(
                    connection,
                    "reauthorization_required",
                    error_code="MCP_OAUTH_TOKEN_BINDING_INVALID",
                )
                raise oauth_error(
                    "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
                    "Authorize this MCP server again.",
                    status_code=409,
                )
            if bundle.expires_at is None or bundle.expires_at > datetime.now(UTC) + safety:
                return bundle.access_token
            if not bundle.refresh_token:
                await mcp_connection_store.set_state(
                    connection,
                    "reauthorization_required",
                    error_code="MCP_OAUTH_REFRESH_TOKEN_MISSING",
                )
                raise oauth_error(
                    "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
                    "Authorize this MCP server again.",
                    status_code=409,
                )
            client_id, resource, endpoint_snapshot, binding_fingerprint = binding
            try:
                response = await oauth_http_request(
                    "POST",
                    endpoint_snapshot.token_endpoint,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/x-www-form-urlencoded",
                    },
                    form_body={
                        "grant_type": "refresh_token",
                        "refresh_token": bundle.refresh_token or "",
                        "client_id": client_id,
                        "resource": resource,
                    },
                )
            except McpOAuthError as exc:
                if exc.code not in {
                    "MCP_OAUTH_ENDPOINT_OUTCOME_UNKNOWN",
                    "MCP_OAUTH_RESPONSE_INVALID",
                    "MCP_OAUTH_RESPONSE_TOO_LARGE",
                }:
                    raise
                await mcp_connection_store.set_state(
                    connection,
                    "reauthorization_required",
                    error_code="MCP_OAUTH_REFRESH_OUTCOME_UNKNOWN",
                )
                raise oauth_error(
                    "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
                    "The refresh outcome was uncertain. Authorize this MCP server again.",
                    status_code=409,
                ) from exc
            if response.status_code in {429} or response.status_code >= 500:
                raise oauth_error(
                    "MCP_OAUTH_REFRESH_UNAVAILABLE",
                    "The authorization server is temporarily unavailable.",
                    status_code=503,
                    retryable=True,
                )
            if response.status_code != 200:
                error_code = None
                with suppress(ValueError):
                    payload = response.json()
                    if isinstance(payload, dict):
                        candidate = payload.get("error")
                        if isinstance(candidate, str) and len(candidate) <= 256:
                            error_code = candidate
                confirmed_auth_failure = (
                    error_code
                    in {"invalid_grant", "invalid_client", "unauthorized_client"}
                    or response.status_code in {401, 403}
                )
                if confirmed_auth_failure:
                    await mcp_connection_store.set_state(
                        connection,
                        "reauthorization_required",
                        error_code="MCP_OAUTH_REFRESH_REJECTED",
                    )
                    raise oauth_error(
                        "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
                        "Authorize this MCP server again.",
                        status_code=409,
                    )
                raise oauth_error(
                    "MCP_OAUTH_REFRESH_FAILED",
                    "The OAuth token could not be refreshed.",
                    status_code=503,
                    retryable=True,
                )
            try:
                payload = response.json()
                rotated = _parse_token_response(
                    payload,
                    requested_scopes=list(connection.oauth_scopes or []),
                    previous_refresh_token=bundle.refresh_token,
                )
                rotated = rotated.model_copy(
                    update={"binding_fingerprint": binding_fingerprint}
                )
            except (ValueError, McpOAuthError) as exc:
                await mcp_connection_store.set_state(
                    connection,
                    "reauthorization_required",
                    error_code="MCP_OAUTH_REFRESH_OUTCOME_UNKNOWN",
                )
                raise oauth_error(
                    "MCP_OAUTH_REAUTHORIZATION_REQUIRED",
                    "The refresh response was unusable. Authorize this MCP server again.",
                    status_code=409,
                ) from exc
            try:
                await self._save(workspace_id, server_id, owner_id, rotated)
            except Exception as exc:
                await self.best_effort_revoke_bundle(
                    endpoint_snapshot=endpoint_snapshot,
                    client_id=client_id,
                    bundle=rotated,
                )
                await mcp_connection_store.set_state(
                    connection,
                    "reauthorization_required",
                    error_code="MCP_OAUTH_TOKEN_PERSISTENCE_FAILED",
                )
                raise oauth_error(
                    "MCP_OAUTH_TOKEN_PERSISTENCE_FAILED",
                    "The refreshed token could not be stored securely.",
                    status_code=503,
                ) from exc
            await mcp_connection_store.set_state(
                connection,
                "connected",
                verified_tool_names=list(connection.verified_tool_names or []),
                oauth_scopes=rotated.scopes,
                oauth_token_expires_at=rotated.expires_at,
                oauth_refresh_capable=bool(rotated.refresh_token),
            )
            return rotated.access_token

    async def delete_tokens(
        self,
        workspace_id: str,
        server_id: str,
        owner_id: str,
    ) -> None:
        with suppress(SecretNotFoundError):
            await secret_store.delete_secret(
                oauth_token_secret_name(workspace_id, server_id, owner_id),
                _token_scope(workspace_id),
            )

    async def revoke(
        self,
        *,
        workspace_id: str,
        server_id: str,
        owner_id: str,
        connection,
    ) -> None:
        """Best-effort RFC 7009 revocation without weakening local cleanup."""

        with suppress(Exception):
            binding = connection_token_binding(connection)
            if binding is None:
                return
            client_id, _resource, endpoints, binding_fingerprint = binding
            if not endpoints.revocation_endpoint:
                return
            bundle = await self._load(workspace_id, server_id, owner_id)
            if bundle.binding_fingerprint != binding_fingerprint:
                return
            await self.best_effort_revoke_bundle(
                endpoint_snapshot=endpoints,
                client_id=client_id,
                bundle=bundle,
            )


oauth_token_service = OAuthTokenService()
