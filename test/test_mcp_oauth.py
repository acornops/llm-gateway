from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api.mcp_admin_schemas import McpServerCreateRequest
from app.config.settings import settings
from app.mcp.egress_policy import ValidatedMcpRequestTarget
from app.mcp.oauth.discovery import _resource_matches_server, discover_mcp_oauth
from app.mcp.oauth.errors import McpOAuthError
from app.mcp.oauth.flow_store import OAuthFlowStore
from app.mcp.oauth.models import (
    OAuthDiscoveryResult,
    OAuthEndpointSnapshot,
    OAuthFlowRecord,
    OAuthPreparationRecord,
    OAuthTokenBundle,
)
from app.mcp.oauth.outbound import oauth_http_request
from app.mcp.oauth.registration import (
    public_client_metadata,
    public_client_metadata_fingerprint,
    register_public_client,
)
from app.mcp.oauth.service import (
    _authorization_url,
    _registration_matches_client_metadata,
    complete_authorization,
    prepare_authorization,
    start_authorization,
)
from app.mcp.oauth.token_binding import token_binding_fingerprint
from app.mcp.oauth.tokens import OAuthTokenService, _parse_token_response


@asynccontextmanager
async def _no_lock(*_args, **_kwargs):
    yield


async def _pass_verification(_flow, _bundle, connection):
    return connection


def _test_token_binding_fingerprint() -> str:
    return token_binding_fingerprint(
        endpoint_snapshot=OAuthEndpointSnapshot(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
        ),
        client_id="public-client",
        resource="https://mcp.example/mcp",
    )


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, value: bytes) -> None:
        self.value = value

    async def __aiter__(self):
        yield self.value


def _response(
    status: int,
    body: dict[str, object] | None = None,
    *,
    headers: dict[str, str] | None = None,
    url: str = "https://mcp.example/mcp",
) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        headers=headers,
        request=httpx.Request("GET", url),
    )


def _prm() -> dict[str, object]:
    return {
        "resource": "https://mcp.example/mcp",
        "authorization_servers": ["https://auth.example"],
        "scopes_supported": ["mcp:read"],
    }


def _asm(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "issuer": "https://auth.example",
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://auth.example/token",
        "registration_endpoint": "https://auth.example/register",
        "scopes_supported": ["mcp:read", "offline_access"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
    value.update(overrides)
    return value


@pytest.mark.anyio
async def test_discovery_prefers_cimd_and_uses_challenge_scope() -> None:
    responses = [
        _response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.example/.well-known/'
                    'oauth-protected-resource/mcp", scope="mcp:write"'
                )
            },
        ),
        _response(200, _prm()),
        _response(200, _asm(client_id_metadata_document_supported=True)),
    ]
    validate_egress = AsyncMock()
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(side_effect=responses),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=validate_egress,
        ),
    ):
        result = await discover_mcp_oauth("https://mcp.example/mcp")
    assert result.candidates[0].registration_method == "cimd"
    assert result.candidates[0].scopes == ["mcp:write", "offline_access"]
    assert result.candidates[0].offline_access_requested is True
    assert result.endpoint_snapshots[
        result.candidates[0].issuer
    ].registration_endpoint is None
    assert all(
        call.args[0] != "https://auth.example/register"
        for call in validate_egress.await_args_list
    )


@pytest.mark.parametrize(
    ("server_url", "resource", "expected"),
    [
        ("https://mcp.example/api/v1/mcp", "https://mcp.example/api", True),
        (
            "https://mcp.example/api/v1/mcp?tenant=one",
            "https://mcp.example/api/v1/mcp?tenant=one",
            True,
        ),
        (
            "https://mcp.example/api/v1/mcp?tenant=one",
            "https://mcp.example/api/v1/mcp?tenant=two",
            False,
        ),
        (
            "https://mcp.example/api/v1/mcp",
            "https://mcp.example/api/v1/mcp#fragment",
            False,
        ),
        (
            "https://mcp.example/api/v1/mcp",
            "https://user@mcp.example/api/v1/mcp",
            False,
        ),
    ],
)
def test_resource_matching_rejects_ambiguous_resource_identifiers(
    server_url: str,
    resource: str,
    expected: bool,
) -> None:
    assert _resource_matches_server(server_url, resource) is expected


@pytest.mark.anyio
async def test_discovery_does_not_allow_resource_to_force_offline_access() -> None:
    responses = [
        _response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.example/.well-known/'
                    'oauth-protected-resource/mcp", '
                    'scope="mcp:write offline_access"'
                )
            },
        ),
        _response(200, _prm()),
        _response(
            200,
            _asm(
                client_id_metadata_document_supported=True,
                scopes_supported=["mcp:write"],
            ),
        ),
    ]
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(side_effect=responses),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
    ):
        result = await discover_mcp_oauth("https://mcp.example/mcp")
    assert result.candidates[0].scopes == ["mcp:write"]
    assert result.candidates[0].offline_access_requested is False


@pytest.mark.anyio
async def test_discovery_discloses_resource_supplied_offline_access_when_supported() -> None:
    responses = [
        _response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.example/.well-known/'
                    'oauth-protected-resource/mcp", '
                    'scope="mcp:write offline_access"'
                )
            },
        ),
        _response(200, _prm()),
        _response(200, _asm(client_id_metadata_document_supported=True)),
    ]
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(side_effect=responses),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
    ):
        result = await discover_mcp_oauth("https://mcp.example/mcp")
    assert result.candidates[0].scopes == ["mcp:write", "offline_access"]
    assert result.candidates[0].offline_access_requested is True


@pytest.mark.anyio
async def test_discovery_uses_dcr_only_when_cimd_is_not_advertised() -> None:
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(200, _asm()),
                ]
            ),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
    ):
        result = await discover_mcp_oauth("https://mcp.example/mcp")
    assert result.candidates[0].registration_method == "dcr"


@pytest.mark.anyio
async def test_discovery_rejects_issuer_mismatch() -> None:
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(200, _asm(issuer="https://other.example")),
                ]
            ),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_ISSUER_MISMATCH"


@pytest.mark.anyio
async def test_discovery_rejects_issuer_with_query_component() -> None:
    resource_metadata = _prm()
    resource_metadata["authorization_servers"] = [
        "https://auth.example/tenant?configuration=alternate"
    ]
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, resource_metadata),
                ]
            ),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_METADATA_INVALID"


@pytest.mark.anyio
async def test_discovery_rejects_mismatched_resource() -> None:
    resource_metadata = _prm()
    resource_metadata["resource"] = "https://other.example/mcp"
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, resource_metadata),
                ]
            ),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_RESOURCE_MISMATCH"


@pytest.mark.anyio
async def test_discovery_accepts_unambiguous_single_resource_array() -> None:
    resource_metadata = _prm()
    resource_metadata["resource"] = ["https://mcp.example/mcp"]
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, resource_metadata),
                    _response(200, _asm()),
                ]
            ),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
    ):
        result = await discover_mcp_oauth("https://mcp.example/mcp")
    assert result.resource == "https://mcp.example/mcp"
    assert result.candidates[0].registration_method == "dcr"


@pytest.mark.anyio
async def test_discovery_rejects_ambiguous_resource_array() -> None:
    resource_metadata = _prm()
    resource_metadata["resource"] = [
        "https://mcp.example/mcp",
        "https://other.example/mcp",
    ]
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, resource_metadata),
                    _response(200, resource_metadata),
                ]
            ),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_PROTECTED_RESOURCE_METADATA_MISSING"


@pytest.mark.anyio
async def test_discovery_rejects_cimd_without_public_token_auth() -> None:
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(
                        200,
                        _asm(
                            token_endpoint_auth_methods_supported=[
                                "client_secret_basic"
                            ],
                            client_id_metadata_document_supported=True,
                        ),
                    ),
                ]
            ),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_PUBLIC_CLIENT_UNSUPPORTED"


@pytest.mark.anyio
async def test_discovery_rejects_cimd_when_token_auth_metadata_is_omitted() -> None:
    metadata = _asm(client_id_metadata_document_supported=True)
    metadata.pop("token_endpoint_auth_methods_supported")
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(200, metadata),
                ]
            ),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_PUBLIC_CLIENT_UNSUPPORTED"


@pytest.mark.anyio
async def test_discovery_defers_dcr_public_auth_compatibility_to_registration() -> None:
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(
                        200,
                        _asm(
                            token_endpoint_auth_methods_supported=[
                                "client_secret_basic"
                            ]
                        ),
                    ),
                ]
            ),
        ),
        patch(
            "app.mcp.oauth.discovery.validate_oauth_endpoint_egress",
            new=AsyncMock(),
        ),
    ):
        result = await discover_mcp_oauth("https://mcp.example/mcp")

    assert result.candidates[0].registration_method == "dcr"


@pytest.mark.anyio
async def test_discovery_requires_explicit_code_response_metadata() -> None:
    metadata = _asm()
    metadata.pop("response_types_supported")
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(200, metadata),
                    _response(404),
                ]
            ),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")

    assert (
        exc_info.value.code
        == "MCP_OAUTH_AUTHORIZATION_SERVER_METADATA_MISSING"
    )


@pytest.mark.anyio
async def test_discovery_requires_advertised_pkce_s256() -> None:
    with (
        patch.object(settings, "MCP_OAUTH_ENABLED", True),
        patch(
            "app.mcp.oauth.discovery.oauth_http_request",
            new=AsyncMock(
                side_effect=[
                    _response(401),
                    _response(200, _prm()),
                    _response(200, _asm(code_challenge_methods_supported=["plain"])),
                ]
            ),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await discover_mcp_oauth("https://mcp.example/mcp")
    assert exc_info.value.code == "MCP_OAUTH_PKCE_S256_UNSUPPORTED"


@pytest.mark.anyio
async def test_public_dcr_rejects_client_secrets() -> None:
    response = _response(
        201,
        {
            "client_id": "client-1",
            "client_secret": "must-not-store",
            "token_endpoint_auth_method": "client_secret_basic",
            "redirect_uris": [
                "http://localhost:3000/api/v1/mcp/oauth/callback",
            ],
        },
    )
    with patch(
        "app.mcp.oauth.registration.oauth_http_request",
        new=AsyncMock(return_value=response),
    ), pytest.raises(McpOAuthError) as exc_info:
        await register_public_client(
            method="dcr",
            endpoints=OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
                registration_endpoint="https://auth.example/register",
            ),
            scopes=["mcp:read"],
        )
    assert exc_info.value.code == "MCP_OAUTH_CONFIDENTIAL_CLIENT_UNSUPPORTED"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response_overrides", "expected_code"),
    [
        (
            {"token_endpoint_auth_method": None},
            "MCP_OAUTH_CONFIDENTIAL_CLIENT_UNSUPPORTED",
        ),
        ({"redirect_uris": None}, "MCP_OAUTH_REGISTRATION_REDIRECT_MISMATCH"),
        (
            {"redirect_uris": ["https://attacker.example/callback"]},
            "MCP_OAUTH_REGISTRATION_REDIRECT_MISMATCH",
        ),
        (
            {"grant_types": ["client_credentials"]},
            "MCP_OAUTH_REGISTRATION_INVALID",
        ),
        (
            {"response_types": ["token"]},
            "MCP_OAUTH_REGISTRATION_INVALID",
        ),
        (
            {"client_id": "public-client\r\ninjected"},
            "MCP_OAUTH_REGISTRATION_INVALID",
        ),
    ],
)
async def test_public_dcr_requires_explicit_public_auth_and_exact_redirect(
    response_overrides: dict[str, object],
    expected_code: str,
) -> None:
    body: dict[str, object] = {
        "client_id": "public-client",
        "token_endpoint_auth_method": "none",
        "redirect_uris": [
            "http://localhost:3000/api/v1/mcp/oauth/callback",
        ],
    }
    for key, value in response_overrides.items():
        if value is None:
            body.pop(key)
        else:
            body[key] = value
    with (
        patch(
            "app.mcp.oauth.registration.oauth_http_request",
            new=AsyncMock(return_value=_response(201, body)),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await register_public_client(
            method="dcr",
            endpoints=OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
                registration_endpoint="https://auth.example/register",
            ),
            scopes=["mcp:read"],
        )

    assert exc_info.value.code == expected_code


@pytest.mark.anyio
async def test_public_dcr_is_unauthenticated_and_discards_management_credentials() -> None:
    request = AsyncMock(
        return_value=_response(
            201,
            {
                "client_id": "public-client",
                "token_endpoint_auth_method": "none",
                "redirect_uris": [
                    "http://localhost:3000/api/v1/mcp/oauth/callback",
                ],
                "registration_access_token": "must-not-persist",
                "registration_client_uri": "https://auth.example/manage/client",
            },
        )
    )
    with patch(
        "app.mcp.oauth.registration.oauth_http_request",
        new=request,
    ):
        client_id = await register_public_client(
            method="dcr",
            endpoints=OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
                registration_endpoint="https://auth.example/register",
            ),
            scopes=["mcp:read"],
        )
    assert client_id == "public-client"
    call = request.await_args
    assert call.kwargs["headers"] == {
        "accept": "application/json",
        "content-type": "application/json",
    }
    payload = call.kwargs["json_body"]
    assert payload["token_endpoint_auth_method"] == "none"
    assert payload["scope"] == "mcp:read"
    assert "client_secret" not in payload
    assert "initial_access_token" not in payload


@pytest.mark.anyio
async def test_flow_store_consumes_preparation_once() -> None:
    store = OAuthFlowStore()
    store._redis = None
    record = OAuthPreparationRecord(
        workspace_id="ws-1",
        server_id="server-1",
        owner_id="user-1",
        browser_binding_hash="a" * 64,
        return_path="/workspaces/ws-1",
        resource="https://mcp.example/mcp",
        candidates=[
            {
                "issuer": "https://auth.example",
                "issuer_origin": "https://auth.example",
                "registration_method": "cimd",
                "scopes": [],
                "offline_access_requested": False,
            }
        ],
        endpoint_snapshots={
            "https://auth.example": OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            )
        },
        metadata_fingerprints={"https://auth.example": "f" * 64},
    )
    handle = await store.create_preparation(record)
    assert (await store.consume_preparation(handle)).owner_id == "user-1"
    with pytest.raises(McpOAuthError) as exc_info:
        await store.consume_preparation(handle)
    assert exc_info.value.code == "MCP_OAUTH_FLOW_INVALID"


@pytest.mark.anyio
async def test_flow_store_creates_redis_record_and_disconnect_index_atomically() -> None:
    store = OAuthFlowStore()
    store._redis = AsyncMock()
    store._redis.eval.return_value = 1
    record = OAuthPreparationRecord(
        workspace_id="ws-1",
        server_id="server-1",
        owner_id="user-1",
        browser_binding_hash="a" * 64,
        return_path="/workspaces/ws-1",
        resource="https://mcp.example/mcp",
        candidates=[{
            "issuer": "https://auth.example",
            "issuer_origin": "https://auth.example",
            "registration_method": "cimd",
            "scopes": [],
            "offline_access_requested": False,
        }],
        endpoint_snapshots={
            "https://auth.example": OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            )
        },
        metadata_fingerprints={"https://auth.example": "f" * 64},
    )

    await store.create_preparation(record)

    store._redis.eval.assert_awaited_once()
    store._redis.set.assert_not_awaited()


@pytest.mark.anyio
async def test_prepare_does_not_degrade_an_existing_connected_connection() -> None:
    discovery = OAuthDiscoveryResult(
        resource="https://mcp.example/mcp",
        candidates=[{
            "issuer": "https://auth.example",
            "issuer_origin": "https://auth.example",
            "registration_method": "cimd",
            "scopes": ["mcp:read"],
        }],
        endpoint_snapshots={
            "https://auth.example": OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            )
        },
        metadata_fingerprints={"https://auth.example": "f" * 64},
    )
    existing = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        status="connected",
    )
    upsert = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.discover_mcp_oauth",
            new=AsyncMock(return_value=discovery),
        ),
        patch(
            "app.mcp.oauth.service.oauth_flow_store.create_preparation",
            new=AsyncMock(return_value="p" * 43),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.upsert",
            new=upsert,
        ),
    ):
        handle, _record = await prepare_authorization(
            server=SimpleNamespace(
                id="11111111-1111-4111-8111-111111111111",
                auth_type="oauth",
                credential_mode="individual",
                server_url="https://mcp.example/mcp",
            ),
            workspace_id="ws-1",
            owner_id="user-1",
            browser_binding_hash="a" * 64,
            return_path="/workspaces/ws-1",
        )

    assert handle == "p" * 43
    upsert.assert_not_awaited()


@pytest.mark.anyio
async def test_prepare_cannot_recreate_a_connection_after_concurrent_disconnect() -> None:
    discovery = OAuthDiscoveryResult(
        resource="https://mcp.example/mcp",
        candidates=[{
            "issuer": "https://auth.example",
            "issuer_origin": "https://auth.example",
            "registration_method": "cimd",
            "scopes": ["mcp:read"],
        }],
        endpoint_snapshots={
            "https://auth.example": OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            )
        },
        metadata_fingerprints={"https://auth.example": "f" * 64},
    )
    existing = SimpleNamespace(id="22222222-2222-4222-8222-222222222222")
    create_preparation = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.discover_mcp_oauth",
            new=AsyncMock(return_value=discovery),
        ),
        patch(
            "app.mcp.oauth.service.oauth_flow_store.create_preparation",
            new=create_preparation,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(side_effect=[existing, None]),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await prepare_authorization(
            server=SimpleNamespace(
                id="11111111-1111-4111-8111-111111111111",
                auth_type="oauth",
                credential_mode="individual",
                server_url="https://mcp.example/mcp",
            ),
            workspace_id="ws-1",
            owner_id="user-1",
            browser_binding_hash="a" * 64,
            return_path="/workspaces/ws-1",
        )

    assert exc_info.value.code == "MCP_OAUTH_INSTALLATION_NOT_FOUND"
    create_preparation.assert_not_awaited()


@pytest.mark.anyio
async def test_start_refuses_to_recreate_a_disconnected_connection() -> None:
    record = OAuthPreparationRecord(
        workspace_id="ws-1",
        server_id="11111111-1111-4111-8111-111111111111",
        owner_id="user-1",
        browser_binding_hash="a" * 64,
        return_path="/workspaces/ws-1",
        resource="https://mcp.example/mcp",
        candidates=[{
            "issuer": "https://auth.example",
            "issuer_origin": "https://auth.example",
            "registration_method": "cimd",
            "scopes": [],
        }],
        endpoint_snapshots={
            "https://auth.example": OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            )
        },
        metadata_fingerprints={"https://auth.example": "f" * 64},
    )
    register = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_preparation",
            new=AsyncMock(return_value=record),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.mcp.oauth.service.register_public_client",
            new=register,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await start_authorization(
            preparation_handle="p" * 43,
            workspace_id="ws-1",
            server_id=record.server_id,
            owner_id="user-1",
            browser_binding_hash="a" * 64,
            issuer=None,
            consent_granted=True,
        )

    assert exc_info.value.code == "MCP_OAUTH_FLOW_INVALID"
    register.assert_not_awaited()


@pytest.mark.anyio
async def test_start_binds_pkce_state_callback_and_resource_before_navigation() -> None:
    record = OAuthPreparationRecord(
        workspace_id="ws-1",
        server_id="11111111-1111-4111-8111-111111111111",
        owner_id="user-1",
        browser_binding_hash="a" * 64,
        return_path="/workspaces/ws-1",
        resource="https://mcp.example/mcp",
        candidates=[{
            "issuer": "https://auth.example",
            "issuer_origin": "https://auth.example",
            "registration_method": "cimd",
            "scopes": ["mcp:read"],
        }],
        endpoint_snapshots={
            "https://auth.example": OAuthEndpointSnapshot(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            )
        },
        metadata_fingerprints={"https://auth.example": "f" * 64},
    )
    connection = SimpleNamespace()
    create_flow = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_preparation",
            new=AsyncMock(return_value=record),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.oauth_registration_store.registration_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.oauth_registration_store.get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.mcp.oauth.service.oauth_registration_store.put",
            new=AsyncMock(return_value=SimpleNamespace()),
        ),
        patch(
            "app.mcp.oauth.service.oauth_flow_store.create_flow",
            new=create_flow,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ),
    ):
        authorization_url, state, _changed = await start_authorization(
            preparation_handle="p" * 43,
            workspace_id="ws-1",
            server_id=record.server_id,
            owner_id="user-1",
            browser_binding_hash="a" * 64,
            issuer=None,
            consent_granted=True,
        )

    query = httpx.QueryParams(httpx.URL(authorization_url).query)
    assert query["state"] == state
    assert query["resource"] == "https://mcp.example/mcp"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == "http://localhost:3000/api/v1/mcp/oauth/callback"
    stored_flow = create_flow.await_args.args[1]
    assert stored_flow.code_verifier
    assert stored_flow.browser_binding_hash == "a" * 64


def _flow() -> OAuthFlowRecord:
    return OAuthFlowRecord(
        workspace_id="ws-1",
        server_id="11111111-1111-4111-8111-111111111111",
        owner_id="user-1",
        browser_binding_hash="a" * 64,
        return_path="/workspaces/ws-1",
        resource="https://mcp.example/mcp",
        issuer="https://auth.example",
        client_id="public-client",
        registration_method="dcr",
        scopes=["mcp:read"],
        code_verifier="v" * 64,
        redirect_uri="http://localhost:3000/api/v1/mcp/oauth/callback",
        endpoint_snapshot=OAuthEndpointSnapshot(
            issuer="https://auth.example",
            authorization_endpoint="https://auth.example/authorize",
            token_endpoint="https://auth.example/token",
        ),
        metadata_fingerprint="f" * 64,
    )


@pytest.mark.anyio
async def test_callback_exchange_is_bound_to_user_browser_issuer_and_resource() -> None:
    flow = _flow()
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    bundle = OAuthTokenBundle(
        access_token="access",
        refresh_token="refresh",
        scopes=["mcp:read"],
    )
    exchange = AsyncMock(return_value=bundle)
    set_state = AsyncMock(return_value=connection)
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=exchange,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=set_state,
        ),
    ):
        completed_flow, completed_bundle, _connection = await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer=flow.issuer,
            provider_error=None,
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=_pass_verification,
        )

    assert completed_flow == flow
    assert completed_bundle == bundle
    assert exchange.await_args.kwargs["resource"] == flow.resource
    assert exchange.await_args.kwargs["redirect_uri"] == flow.redirect_uri
    assert exchange.await_args.kwargs["code_verifier"] == flow.code_verifier
    assert set_state.await_args.kwargs["oauth_client_id"] == flow.client_id
    assert (
        set_state.await_args.kwargs["oauth_endpoint_snapshot"]
        == flow.endpoint_snapshot.model_dump()
    )


@pytest.mark.anyio
async def test_callback_exchange_and_verification_share_one_connection_lock() -> None:
    flow = _flow()
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    bundle = OAuthTokenBundle(access_token="access", scopes=["mcp:read"])
    lock_held = False

    @asynccontextmanager
    async def tracked_lock(*_args, **_kwargs):
        nonlocal lock_held
        assert lock_held is False
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    async def verify(_flow, _bundle, current):
        assert lock_held is True
        return current

    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=tracked_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=AsyncMock(return_value=bundle),
        ),
    ):
        await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer=flow.issuer,
            provider_error=None,
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=verify,
        )

    assert lock_held is False


@pytest.mark.anyio
async def test_callback_revokes_new_bundle_if_connection_disappears_after_exchange() -> None:
    flow = _flow()
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    bundle = OAuthTokenBundle(
        access_token="access",
        refresh_token="refresh",
        scopes=["mcp:read"],
    )
    revoke = AsyncMock()
    delete = AsyncMock()
    verify = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=AsyncMock(return_value=bundle),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.best_effort_revoke_bundle",
            new=revoke,
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.delete_tokens",
            new=delete,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer=flow.issuer,
            provider_error=None,
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=verify,
        )

    assert exc_info.value.code == "MCP_OAUTH_INSTALLATION_NOT_FOUND"
    revoke.assert_awaited_once_with(
        endpoint_snapshot=flow.endpoint_snapshot,
        client_id=flow.client_id,
        bundle=bundle,
    )
    delete.assert_awaited_once()
    verify.assert_not_awaited()


@pytest.mark.anyio
async def test_callback_cleans_up_tokens_if_binding_metadata_cannot_be_persisted() -> None:
    flow = _flow()
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    bundle = OAuthTokenBundle(
        access_token="access",
        refresh_token="refresh",
        scopes=["mcp:read"],
    )
    revoke = AsyncMock()
    delete = AsyncMock()
    set_state = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=set_state,
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=AsyncMock(return_value=bundle),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.best_effort_revoke_bundle",
            new=revoke,
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.delete_tokens",
            new=delete,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer=flow.issuer,
            provider_error=None,
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=_pass_verification,
        )

    assert exc_info.value.code == "MCP_OAUTH_TOKEN_PERSISTENCE_FAILED"
    revoke.assert_awaited_once_with(
        endpoint_snapshot=flow.endpoint_snapshot,
        client_id=flow.client_id,
        bundle=bundle,
    )
    delete.assert_awaited_once_with(
        flow.workspace_id,
        flow.server_id,
        flow.owner_id,
    )


@pytest.mark.anyio
async def test_callback_rejects_user_or_browser_mismatch_before_exchange() -> None:
    flow = _flow()
    exchange = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=exchange,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer=flow.issuer,
            provider_error=None,
            owner_id="other-user",
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=_pass_verification,
        )

    assert exc_info.value.code == "MCP_OAUTH_FLOW_BINDING_MISMATCH"
    exchange.assert_not_awaited()


@pytest.mark.anyio
async def test_callback_rejects_rfc9207_issuer_mismatch_before_exchange() -> None:
    flow = _flow()
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    exchange = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=exchange,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer="https://other.example",
            provider_error=None,
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=_pass_verification,
        )

    assert exc_info.value.code == "MCP_OAUTH_ISSUER_MISMATCH"
    exchange.assert_not_awaited()


@pytest.mark.anyio
async def test_callback_requires_issuer_when_advertised_before_exchange() -> None:
    flow = _flow()
    flow.endpoint_snapshot.authorization_response_iss_parameter_supported = True
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    exchange = AsyncMock()
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.oauth_token_service.exchange_authorization_code",
            new=exchange,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await complete_authorization(
            code="authorization-code",
            state="s" * 43,
            issuer=None,
            provider_error=None,
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=_pass_verification,
        )

    assert exc_info.value.code == "MCP_OAUTH_ISSUER_MISMATCH"
    exchange.assert_not_awaited()


@pytest.mark.anyio
async def test_callback_validates_issuer_before_accepting_provider_denial() -> None:
    flow = _flow()
    connection = SimpleNamespace(
        status="pending_authorization",
        oauth_issuer=flow.issuer,
    )
    with (
        patch(
            "app.mcp.oauth.service.oauth_flow_store.consume_flow",
            new=AsyncMock(return_value=flow),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.mutation_lock",
            new=_no_lock,
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.service.mcp_connection_store.set_state",
            new=AsyncMock(return_value=connection),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await complete_authorization(
            code=None,
            state="s" * 43,
            issuer="https://other.example",
            provider_error="access_denied",
            owner_id=flow.owner_id,
            browser_binding_hash=flow.browser_binding_hash,
            verify_connection=_pass_verification,
        )

    assert exc_info.value.code == "MCP_OAUTH_ISSUER_MISMATCH"


@pytest.mark.anyio
async def test_oauth_http_is_pinned_does_not_redirect_and_overrides_reserved_headers() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://attacker.example"},
            stream=_AsyncBytes(b""),
        )

    target = ValidatedMcpRequestTarget(
        original_url="https://auth.example/token",
        connection_url="https://203.0.113.10/token",
        host_header="auth.example",
        extensions={"sni_hostname": "auth.example"},
    )
    with (
        patch(
            "app.mcp.oauth.outbound.prepare_mcp_egress_request",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "app.mcp.oauth.outbound.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ),
    ):
        response = await oauth_http_request(
            "GET",
            target.original_url,
            headers={"host": "attacker.example", "accept-encoding": "gzip"},
        )

    assert response.status_code == 302
    assert len(requests) == 1
    assert requests[0].url.host == "203.0.113.10"
    assert requests[0].headers["host"] == "auth.example"
    assert requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.anyio
async def test_oauth_http_rejects_compressed_and_oversized_responses() -> None:
    target = ValidatedMcpRequestTarget(
        original_url="https://auth.example/token",
        connection_url="https://203.0.113.10/token",
        host_header="auth.example",
        extensions={"sni_hostname": "auth.example"},
    )

    async def compressed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=_AsyncBytes(b"compressed"),
        )

    with (
        patch(
            "app.mcp.oauth.outbound.prepare_mcp_egress_request",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "app.mcp.oauth.outbound.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(compressed),
        ),
        pytest.raises(McpOAuthError) as compressed_error,
    ):
        await oauth_http_request("GET", target.original_url)
    assert compressed_error.value.code == "MCP_OAUTH_RESPONSE_INVALID"

    async def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_AsyncBytes(b"123456789"))

    with (
        patch.object(settings, "MCP_OAUTH_MAX_RESPONSE_BYTES", 8),
        patch(
            "app.mcp.oauth.outbound.prepare_mcp_egress_request",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "app.mcp.oauth.outbound.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(oversized),
        ),
        pytest.raises(McpOAuthError) as oversized_error,
    ):
        await oauth_http_request("GET", target.original_url)
    assert oversized_error.value.code == "MCP_OAUTH_RESPONSE_TOO_LARGE"

    async def malformed_length(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "invalid"},
            stream=_AsyncBytes(b""),
        )

    with (
        patch(
            "app.mcp.oauth.outbound.prepare_mcp_egress_request",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "app.mcp.oauth.outbound.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(malformed_length),
        ),
        pytest.raises(McpOAuthError) as malformed_error,
    ):
        await oauth_http_request("GET", target.original_url)
    assert malformed_error.value.code == "MCP_OAUTH_RESPONSE_INVALID"


def test_authorization_url_preserves_fixed_query_and_rejects_parameter_collision() -> None:
    result = _authorization_url(
        "https://auth.example/authorize?audience=gitlab",
        {"client_id": "client-1", "state": "state-1"},
    )
    assert "audience=gitlab" in result
    assert "client_id=client-1" in result
    with pytest.raises(McpOAuthError) as exc_info:
        _authorization_url(
            "https://auth.example/authorize?state=attacker",
            {"client_id": "client-1", "state": "state-1"},
        )
    assert exc_info.value.code == "MCP_OAUTH_METADATA_INVALID"


def test_registration_reuse_is_bound_to_public_client_metadata() -> None:
    fingerprint = public_client_metadata_fingerprint("dcr", ["mcp:read"])
    registration = SimpleNamespace(
        registration_method="dcr",
        client_metadata_fingerprint=fingerprint,
    )
    assert _registration_matches_client_metadata(
        registration,
        method="dcr",
        fingerprint=fingerprint,
    )
    assert not _registration_matches_client_metadata(
        registration,
        method="dcr",
        fingerprint=public_client_metadata_fingerprint("dcr", ["mcp:write"]),
    )


def test_cimd_fingerprint_uses_the_public_document_without_per_server_scopes() -> None:
    assert public_client_metadata_fingerprint(
        "cimd",
        ["mcp:read"],
    ) == public_client_metadata_fingerprint("cimd", ["mcp:write"])


def test_token_response_requires_an_explicit_bearer_token_type() -> None:
    with pytest.raises(McpOAuthError) as exc_info:
        _parse_token_response(
            {"access_token": "opaque"},
            requested_scopes=[],
        )
    assert exc_info.value.code == "MCP_OAUTH_TOKEN_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "opaque\r\ninjected", "token_type": "Bearer"},
        {
            "access_token": "opaque",
            "refresh_token": "refresh\u0000token",
            "token_type": "Bearer",
        },
        {"access_token": "opaque-\u2603", "token_type": "Bearer"},
    ],
)
def test_token_response_rejects_values_unsafe_for_oauth_transport(
    payload: dict[str, object],
) -> None:
    with pytest.raises(McpOAuthError) as exc_info:
        _parse_token_response(payload, requested_scopes=[])
    assert exc_info.value.code == "MCP_OAUTH_TOKEN_RESPONSE_INVALID"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stored_value",
    [
        (
            '{"version":1,"access_token":"opaque\\r\\ninjected",'
            '"refresh_token":null,"token_type":"Bearer",'
            '"expires_at":null,"scopes":[]}'
        ),
        (
            '{"version":1,"access_token":"opaque",'
            '"refresh_token":null,"token_type":"Bearer",'
            '"expires_at":"2026-01-01T00:00:00","scopes":[]}'
        ),
        (
            '{"version":1,"access_token":"opaque",'
            '"refresh_token":null,"token_type":"Bearer",'
            '"expires_at":null,"scopes":["bad scope"]}'
        ),
    ],
)
async def test_stored_token_bundle_is_revalidated_before_use(
    stored_value: str,
) -> None:
    service = OAuthTokenService()
    with (
        patch(
            "app.mcp.oauth.tokens.secret_store.get_secret",
            new=AsyncMock(return_value=stored_value),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await service._load(
            "ws-1",
            "11111111-1111-4111-8111-111111111111",
            "user-1",
        )

    assert exc_info.value.code == "MCP_OAUTH_TOKEN_SECRET_INVALID"


@pytest.mark.anyio
async def test_invalid_encrypted_token_transitions_to_reauthorization() -> None:
    service = OAuthTokenService()
    connection = SimpleNamespace()
    set_state = AsyncMock()
    with (
        patch.object(
            service,
            "_load",
            new=AsyncMock(
                side_effect=McpOAuthError(
                    "MCP_OAUTH_TOKEN_SECRET_INVALID",
                    "invalid",
                    status_code=409,
                )
            ),
        ),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.set_state",
            new=set_state,
        ),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await service.access_token(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            connection=connection,
        )

    assert exc_info.value.code == "MCP_OAUTH_REAUTHORIZATION_REQUIRED"
    assert set_state.await_args.args[1] == "reauthorization_required"
    assert set_state.await_args.kwargs["error_code"] == "MCP_OAUTH_TOKEN_SECRET_INVALID"


@pytest.mark.anyio
async def test_code_exchange_revokes_issued_bundle_when_secure_storage_fails() -> None:
    service = OAuthTokenService()
    connection = SimpleNamespace()
    request = AsyncMock(
        side_effect=[
            _response(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
                url="https://auth.example/token",
            ),
            _response(200, {}, url="https://auth.example/revoke"),
            _response(200, {}, url="https://auth.example/revoke"),
        ]
    )
    set_state = AsyncMock()
    endpoints = OAuthEndpointSnapshot(
        issuer="https://auth.example",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        revocation_endpoint="https://auth.example/revoke",
    )
    with (
        patch.object(
            service,
            "_save",
            new=AsyncMock(side_effect=RuntimeError("secure storage unavailable")),
        ),
        patch("app.mcp.oauth.tokens.oauth_http_request", new=request),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch("app.mcp.oauth.tokens.mcp_connection_store.set_state", new=set_state),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await service.exchange_authorization_code(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            client_id="public-client",
            code="authorization-code",
            code_verifier="v" * 64,
            redirect_uri="https://console.example/api/v1/mcp/oauth/callback",
            resource="https://mcp.example/mcp",
            scopes=["mcp:read"],
            endpoint_snapshot=endpoints,
        )

    assert exc_info.value.code == "MCP_OAUTH_TOKEN_PERSISTENCE_FAILED"
    assert request.await_count == 3
    revocation = request.await_args_list[1]
    assert revocation.args == ("POST", "https://auth.example/revoke")
    assert revocation.kwargs["form_body"] == {
        "token": "new-refresh",
        "token_type_hint": "refresh_token",
        "client_id": "public-client",
    }
    access_revocation = request.await_args_list[2]
    assert access_revocation.kwargs["form_body"] == {
        "token": "new-access",
        "token_type_hint": "access_token",
        "client_id": "public-client",
    }
    assert set_state.await_args.args[1] == "reauthorization_required"


@pytest.mark.anyio
async def test_best_effort_revoke_never_blocks_local_cleanup() -> None:
    service = OAuthTokenService()
    connection = SimpleNamespace(
        oauth_client_id="public-client",
        oauth_endpoint_snapshot={"invalid": "metadata"},
    )
    await service.revoke(
        workspace_id="ws-1",
        server_id="11111111-1111-4111-8111-111111111111",
        owner_id="user-1",
        connection=connection,
    )


@pytest.mark.anyio
async def test_revocation_never_sends_a_token_with_mismatched_binding() -> None:
    service = OAuthTokenService()
    connection = SimpleNamespace(
        oauth_client_id="different-client",
        oauth_resource="https://mcp.example/mcp",
        oauth_endpoint_snapshot={
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
            "revocation_endpoint": "https://auth.example/revoke",
        },
    )
    request = AsyncMock()
    with (
        patch.object(
            service,
            "_load",
            new=AsyncMock(
                return_value=OAuthTokenBundle(
                    access_token="opaque",
                    binding_fingerprint=_test_token_binding_fingerprint(),
                )
            ),
        ),
        patch("app.mcp.oauth.tokens.oauth_http_request", new=request),
    ):
        await service.revoke(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            connection=connection,
        )

    request.assert_not_awaited()


@pytest.mark.anyio
async def test_refresh_persists_rotation_before_releasing_connected_state() -> None:
    service = OAuthTokenService()
    expired = OAuthTokenBundle(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        scopes=["mcp:read"],
        binding_fingerprint=_test_token_binding_fingerprint(),
    )
    connection = SimpleNamespace(
        oauth_issuer="https://auth.example",
        oauth_client_id="public-client",
        oauth_resource="https://mcp.example/mcp",
        oauth_endpoint_snapshot={
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
        },
        oauth_scopes=["mcp:read"],
        verified_tool_names=["tools/list"],
    )
    events: list[str] = []
    save = AsyncMock(side_effect=lambda *_args: events.append("save"))
    set_state = AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("state"))
    request = AsyncMock(
        return_value=_response(
            200,
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "mcp:read",
            },
        )
    )
    with (
        patch.object(service, "_load", new=AsyncMock(return_value=expired)),
        patch.object(service, "_save", new=save),
        patch("app.mcp.oauth.tokens.oauth_http_request", new=request),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch("app.mcp.oauth.tokens.mcp_connection_store.set_state", new=set_state),
    ):
        token = await service.access_token(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            connection=connection,
        )
    assert token == "new-access"
    assert events == ["save", "state"]
    assert request.await_args.args == ("POST", "https://auth.example/token")
    assert request.await_args.kwargs["form_body"]["client_id"] == "public-client"
    assert request.await_args.kwargs["form_body"]["resource"] == "https://mcp.example/mcp"
    assert save.await_args.args[3].refresh_token == "new-refresh"
    assert (
        save.await_args.args[3].binding_fingerprint
        == _test_token_binding_fingerprint()
    )


@pytest.mark.anyio
async def test_token_use_fails_closed_when_secret_and_connection_bindings_differ() -> None:
    service = OAuthTokenService()
    bundle = OAuthTokenBundle(
        access_token="opaque",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        binding_fingerprint=_test_token_binding_fingerprint(),
    )
    connection = SimpleNamespace(
        oauth_client_id="different-client",
        oauth_resource="https://mcp.example/mcp",
        oauth_endpoint_snapshot={
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
        },
    )
    set_state = AsyncMock()
    with (
        patch.object(service, "_load", new=AsyncMock(return_value=bundle)),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.set_state",
            new=set_state,
        ),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await service.access_token(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            connection=connection,
        )

    assert exc_info.value.code == "MCP_OAUTH_REAUTHORIZATION_REQUIRED"
    assert set_state.await_args.kwargs["error_code"] == "MCP_OAUTH_TOKEN_BINDING_INVALID"


@pytest.mark.anyio
async def test_expired_token_decision_is_rechecked_under_connection_lock() -> None:
    service = OAuthTokenService()
    stale = OAuthTokenBundle(
        access_token="stale-access",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        binding_fingerprint=_test_token_binding_fingerprint(),
    )
    current = OAuthTokenBundle(
        access_token="current-access",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        binding_fingerprint=_test_token_binding_fingerprint(),
    )
    connection = SimpleNamespace(
        oauth_client_id="public-client",
        oauth_resource="https://mcp.example/mcp",
        oauth_endpoint_snapshot={
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
        },
    )
    set_state = AsyncMock()
    load = AsyncMock(side_effect=[stale, current])
    with (
        patch.object(service, "_load", new=load),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.set_state",
            new=set_state,
        ),
    ):
        token = await service.access_token(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            connection=SimpleNamespace(),
        )

    assert token == "current-access"
    assert load.await_count == 2
    set_state.assert_not_awaited()


@pytest.mark.anyio
async def test_ambiguous_refresh_requires_reauthorization_without_retry() -> None:
    service = OAuthTokenService()
    expired = OAuthTokenBundle(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        binding_fingerprint=_test_token_binding_fingerprint(),
    )
    connection = SimpleNamespace(
        oauth_issuer="https://auth.example",
        oauth_client_id="public-client",
        oauth_resource="https://mcp.example/mcp",
        oauth_endpoint_snapshot={
            "issuer": "https://auth.example",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
        },
        oauth_scopes=[],
        verified_tool_names=[],
    )
    request = AsyncMock(
        side_effect=McpOAuthError(
            "MCP_OAUTH_ENDPOINT_OUTCOME_UNKNOWN",
            "unknown",
            status_code=503,
            retryable=True,
        )
    )
    set_state = AsyncMock()
    with (
        patch.object(service, "_load", new=AsyncMock(return_value=expired)),
        patch("app.mcp.oauth.tokens.oauth_http_request", new=request),
        patch(
            "app.mcp.oauth.tokens.mcp_connection_store.get",
            new=AsyncMock(return_value=connection),
        ),
        patch("app.mcp.oauth.tokens.mcp_connection_store.set_state", new=set_state),
        pytest.raises(McpOAuthError) as exc_info,
    ):
        await service.access_token(
            workspace_id="ws-1",
            server_id="11111111-1111-4111-8111-111111111111",
            owner_id="user-1",
            connection=connection,
        )
    assert exc_info.value.code == "MCP_OAUTH_REAUTHORIZATION_REQUIRED"
    assert request.await_count == 1
    assert set_state.await_args.args[1] == "reauthorization_required"
    assert (
        set_state.await_args.kwargs["error_code"]
        == "MCP_OAUTH_REFRESH_OUTCOME_UNKNOWN"
    )


def test_oauth_installations_are_individual_and_secret_free() -> None:
    request = McpServerCreateRequest(
        workspace_id="ws-1",
        target_id="target-1",
        target_type="kubernetes",
        server_name="GitLab",
        server_url="https://gitlab.example/api/v4/mcp",
        auth_type="oauth",
        credential_mode="individual",
    )
    assert request.auth_header_name is None
    with pytest.raises(ValueError):
        McpServerCreateRequest(
            workspace_id="ws-1",
            target_id="target-1",
            target_type="kubernetes",
            server_name="GitLab",
            server_url="https://gitlab.example/api/v4/mcp",
            auth_type="oauth",
            credential_mode="workspace",
        )


def test_public_client_metadata_has_no_secret_or_tenant_state() -> None:
    metadata = public_client_metadata(["mcp:read"])
    serialized = str(metadata)
    assert metadata["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in serialized
    assert "workspace" not in serialized
    assert metadata["redirect_uris"] == [
        "http://localhost:3000/api/v1/mcp/oauth/callback"
    ]
