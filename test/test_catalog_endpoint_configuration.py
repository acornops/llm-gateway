from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.catalog_endpoint_configuration import resolve_catalog_endpoint
from app.catalog.schemas import CatalogAgentMcpImportRequest


def _artifact(*, public_header_templates: list[dict[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        compatible=True,
        incompatibility_reason=None,
        remote_endpoints=[
            {
                "url": "https://mcp.example.test/mcp",
                "supported": True,
                "configurationFields": [],
                "publicHeaderTemplates": public_header_templates,
                "secretHeaderNames": [],
                "requiresPersonalAuth": False,
            }
        ],
    )


def _request(*, public_headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        remote_endpoint="https://mcp.example.test/mcp",
        endpoint_configuration={},
        public_headers=public_headers,
    )


def test_catalog_request_rejects_forbidden_public_headers() -> None:
    with pytest.raises(ValidationError, match="may not contain credentials"):
        CatalogAgentMcpImportRequest.model_validate(
            {
                "workspace_id": "workspace-1",
                "artifact": {"artifact_id": "artifact-1"},
                "version": "1.0.0",
                "remote_endpoint": "https://mcp.example.test/mcp",
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
async def test_catalog_allowed_headers_survive_final_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.catalog_endpoint_configuration.validate_mcp_server_url",
        AsyncMock(return_value=None),
    )

    resolved = await resolve_catalog_endpoint(
        _artifact(
            public_header_templates=[
                {"name": "x-registry-region", "value": "us-east"}
            ]
        ),
        _request(public_headers={"x-client-version": "2026-07"}),
    )

    assert resolved.public_headers == {
        "x-client-version": "2026-07",
        "x-registry-region": "us-east",
    }
