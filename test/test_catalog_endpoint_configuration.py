from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.catalog_endpoint_configuration import resolve_catalog_endpoint
from app.catalog.adapter import normalize_mcp_registry_entry
from app.catalog.schemas import CatalogAgentMcpImportRequest


def _artifact(
    *,
    public_header_templates: list[dict[str, str]],
    endpoint_overrides: dict[str, object] | None = None,
) -> SimpleNamespace:
    endpoint = {
        "url": "https://mcp.example.test/mcp",
        "supported": True,
        "configurationFields": [],
        "publicHeaderTemplates": public_header_templates,
        "secretHeaderNames": [],
    }
    endpoint.update(endpoint_overrides or {})
    return SimpleNamespace(
        compatible=True,
        incompatibility_reason=None,
        remote_endpoints=[endpoint],
    )


def _request(*, public_headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        remote_endpoint="https://mcp.example.test/mcp",
        endpoint_configuration={},
        public_headers=public_headers,
        credential_mode=None,
    )


def test_catalog_request_rejects_forbidden_public_headers() -> None:
    with pytest.raises(ValidationError, match="may not contain credentials"):
        CatalogAgentMcpImportRequest.model_validate(
            {
                "workspace_id": "workspace-1",
                "artifact": {"artifact_id": "artifact-1"},
                "version": "1.0.0",
                "remote_endpoint": "https://mcp.example.test/mcp",
                "scope_type": "agent",
                "agent_id": "agent-1",
                "public_headers": {"Authorization": "Bearer must-not-persist"},
            }
        )


@pytest.mark.anyio
async def test_catalog_fixed_forbidden_header_is_rejected_after_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException) as raised:
        await resolve_catalog_endpoint(
            _artifact(
                public_header_templates=[
                    {"name": "Cookie", "value": "registry-value-must-not-persist"}
                ]
            ),
            _request(public_headers={"x-client-version": "2026-07"}),
        )

    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "INVALID_MCP_PUBLIC_HEADERS"
    assert "registry-value-must-not-persist" not in str(raised.value.detail)


@pytest.mark.anyio
async def test_catalog_endpoint_rejects_secret_query_before_egress_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate_egress = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        validate_egress,
    )
    endpoint = "https://mcp.example.test/mcp?api_key=must-not-persist"
    request = _request()
    request.remote_endpoint = endpoint

    with pytest.raises(HTTPException) as raised:
        await resolve_catalog_endpoint(
            _artifact(
                public_header_templates=[],
                endpoint_overrides={"url": endpoint},
            ),
            request,
        )

    assert raised.value.status_code == 400
    validate_egress.assert_not_awaited()


@pytest.mark.anyio
async def test_catalog_allowed_headers_survive_final_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        AsyncMock(return_value=None),
    )

    resolved = await resolve_catalog_endpoint(
        _artifact(public_header_templates=[{"name": "x-registry-region", "value": "us-east"}]),
        _request(public_headers={"x-client-version": "2026-07"}),
    )

    assert resolved.public_headers == {
        "x-client-version": "2026-07",
        "x-registry-region": "us-east",
    }


@pytest.mark.anyio
async def test_adapter_credential_free_endpoint_resolves_with_none_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        AsyncMock(return_value=None),
    )
    artifact = normalize_mcp_registry_entry(
        {
            "server": {
                "name": "io.example/public",
                "description": "Public MCP server",
                "version": "1.0.0",
                "remotes": [
                    {
                        "type": "streamable-http",
                        "url": "https://mcp.example.test/mcp",
                    }
                ],
            }
        }
    )

    resolved = await resolve_catalog_endpoint(artifact, _request())

    assert resolved.supported_credential_modes == ("none",)
    assert resolved.credential_mode == "none"
    assert resolved.credential_header_name is None
    assert resolved.credential_auth_type == "none"
    assert resolved.credential_auth_header_prefix == ""


@pytest.mark.anyio
async def test_legacy_credential_endpoint_infers_secret_backed_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        AsyncMock(return_value=None),
    )

    resolved = await resolve_catalog_endpoint(
        _artifact(
            public_header_templates=[],
            endpoint_overrides={"secretHeaderNames": ["Authorization"]},
        ),
        _request(),
    )

    assert resolved.supported_credential_modes == ("workspace", "individual")
    assert resolved.credential_mode == "workspace"
    assert resolved.credential_auth_type == "bearer_token"
    assert resolved.credential_auth_header_prefix == "Bearer "


@pytest.mark.anyio
async def test_catalog_rejects_credential_modes_that_contradict_endpoint_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException, match="credential mode none"):
        await resolve_catalog_endpoint(
            _artifact(
                public_header_templates=[],
                endpoint_overrides={
                    "secretHeaderNames": ["Authorization"],
                    "supportedCredentialModes": ["none"],
                },
            ),
            _request(),
        )

    with pytest.raises(HTTPException, match="must use credential mode none"):
        await resolve_catalog_endpoint(
            _artifact(
                public_header_templates=[],
                endpoint_overrides={"supportedCredentialModes": ["workspace"]},
            ),
            _request(),
        )
