import json
from datetime import UTC, datetime

import httpx
import pytest

from app.catalog.adapter import (
    CatalogAdapterError,
    McpRegistryV01Adapter,
    normalize_mcp_registry_entry,
)
from app.catalog.schemas import (
    CatalogMcpImportRequest,
    CatalogSourceCreateRequest,
    CatalogSourcePatchRequest,
)
from app.observability.metrics import GATEWAY_CATALOG_IMPORTS_TOTAL


def registry_entry(*, version: str = "1.2.3", remotes=None):
    return {
        "server": {
            "name": "io.example/operations",
            "title": "Operations",
            "description": "Operate internal systems",
            "version": version,
            "remotes": remotes
            if remotes is not None
            else [
                {
                    "type": "streamable-http",
                    "url": "https://mcp.example.com/mcp",
                    "headers": [
                        {"name": "Authorization", "isSecret": True},
                        {"name": "X-Tenant", "isSecret": False},
                    ],
                }
            ],
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "publishedAt": "2026-07-01T00:00:00Z",
                "updatedAt": "2026-07-02T00:00:00Z",
            }
        },
    }


def test_normalizes_streamable_http_endpoint_and_secret_free_provenance() -> None:
    artifact = normalize_mcp_registry_entry(registry_entry())

    assert artifact.name == "io.example/operations"
    assert artifact.version == "1.2.3"
    assert artifact.compatible is True
    assert artifact.digest.startswith("sha256:")
    assert artifact.remote_endpoints == [
        {
            "type": "streamable-http",
            "url": "https://mcp.example.com/mcp",
            "supported": True,
            "requiresConfiguration": False,
            "requiresPersonalAuth": True,
            "headerNames": ["Authorization", "X-Tenant"],
            "secretHeaderNames": ["Authorization"],
            "personalAuthHeaderPrefix": None,
            "configurationFields": [],
            "publicHeaderTemplates": [],
        }
    ]
    assert "Authorization" not in json.dumps(artifact.metadata)


def test_marks_stdio_only_artifact_incompatible() -> None:
    artifact = normalize_mcp_registry_entry(
        registry_entry(remotes=[{"type": "stdio", "command": "server"}])
    )

    assert artifact.compatible is False
    assert artifact.remote_endpoints == []
    assert "Streamable HTTP" in str(artifact.incompatibility_reason)


def test_normalizes_non_secret_url_and_header_variables() -> None:
    artifact = normalize_mcp_registry_entry(
        registry_entry(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://{region}.example.com/mcp",
                    "variables": {
                        "region": {
                            "description": "Deployment region",
                            "isRequired": True,
                            "choices": ["us", "eu"],
                        }
                    },
                    "headers": [
                        {
                            "name": "X-Tenant",
                            "value": "{tenant}",
                            "variables": {
                                "tenant": {"isRequired": True, "placeholder": "team-a"}
                            },
                        },
                        {
                            "name": "Authorization",
                            "value": "Bearer {token}",
                            "variables": {"token": {"isSecret": True, "isRequired": True}},
                        },
                    ],
                }
            ]
        )
    )

    endpoint = artifact.remote_endpoints[0]
    assert artifact.compatible is True
    assert endpoint["url"] == "https://{region}.example.com/mcp"
    assert endpoint["personalAuthHeaderPrefix"] == "Bearer "
    assert endpoint["publicHeaderTemplates"] == [{"name": "X-Tenant", "value": "{tenant}"}]
    assert {field["name"] for field in endpoint["configurationFields"]} == {
        "region",
        "tenant",
        "token",
    }
    token_field = next(
        field
        for field in endpoint["configurationFields"]
        if field["name"] == "token"
    )
    assert token_field == {
        "name": "token",
        "location": "header",
        "headerName": "Authorization",
        "required": True,
        "secret": True,
        "format": "string",
    }


def test_rejects_malformed_registry_wrapper() -> None:
    with pytest.raises(CatalogAdapterError, match="server response wrapper"):
        normalize_mcp_registry_entry({"name": "unwrapped"})


def _import_payload() -> dict[str, object]:
    return {
        "workspace_id": "workspace-a",
        "artifact": {"artifact_id": "artifact-a"},
        "version": "1.2.3",
        "remote_endpoint": "https://mcp.example.com/mcp",
    }


def test_catalog_import_accepts_legacy_agent_shape() -> None:
    request = CatalogMcpImportRequest.model_validate(
        {**_import_payload(), "agent_id": "agent-a"}
    ).root

    assert request.scope_type == "agent"
    assert request.agent_id == "agent-a"


def test_catalog_import_accepts_target_destination_without_agent_constraints() -> None:
    request = CatalogMcpImportRequest.model_validate(
        {
            **_import_payload(),
            "scope_type": "target",
            "target_id": "cluster-a",
            "target_type": "kubernetes",
        }
    ).root

    assert request.scope_type == "target"
    assert request.target_id == "cluster-a"
    assert request.target_type == "kubernetes"

    with pytest.raises(ValueError, match="target_constraints"):
        CatalogMcpImportRequest.model_validate(
            {
                **_import_payload(),
                "scope_type": "target",
                "target_id": "cluster-a",
                "target_type": "kubernetes",
                "target_constraints": {"target_ids": ["cluster-a"]},
            }
        )


def test_catalog_import_metric_has_only_bounded_labels() -> None:
    assert GATEWAY_CATALOG_IMPORTS_TOTAL._labelnames == (
        "scope_type",
        "operation",
        "outcome",
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://registry.example",
        "https://user:password@registry.example",
        "https://registry.example/v0.1",
        "https://registry.example/root/v0.1/",
        "https://registry.example?token=secret",
        "https://registry.example#servers",
    ],
)
def test_catalog_source_rejects_non_root_or_unsafe_registry_urls(
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        CatalogSourceCreateRequest.model_validate(
            {
                "workspace_id": "workspace-a",
                "display_name": "Internal registry",
                "base_url": base_url,
            }
        )


def test_catalog_source_normalizes_registry_path_prefix() -> None:
    request = CatalogSourceCreateRequest.model_validate(
        {
            "workspace_id": "workspace-a",
            "display_name": " Internal   registry ",
            "base_url": "https://registry.example/platform/mcp/",
        }
    )

    assert request.display_name == "Internal registry"
    assert request.base_url == "https://registry.example/platform/mcp"


def test_catalog_source_patch_auth_contract_preserves_omitted_auth() -> None:
    patch = CatalogSourcePatchRequest.model_validate({"enabled": False})

    assert "auth" not in patch.model_fields_set

    with pytest.raises(ValueError, match="credential is required"):
        CatalogSourcePatchRequest.model_validate(
            {"auth": {"type": "bearer_token"}}
        )


@pytest.mark.anyio
async def test_adapter_honors_cursor_incremental_sync_and_version_resolution() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v0.1/servers":
            return httpx.Response(
                200,
                json={
                    "servers": [registry_entry()],
                    "metadata": {"nextCursor": "page-2"},
                },
            )
        if request.url.path.endswith("/versions/2.0.0"):
            return httpx.Response(200, json=registry_entry(version="2.0.0"))
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = McpRegistryV01Adapter("https://registry.example", client=client)
        page = await adapter.list_updated(
            cursor="page-1",
            updated_since=datetime(2026, 7, 1, tzinfo=UTC),
        )
        resolved = await adapter.fetch_artifact("io.example/operations", "2.0.0")

    assert page.next_cursor == "page-2"
    assert requests[0].url.params["cursor"] == "page-1"
    assert requests[0].url.params["updated_since"] == "2026-07-01T00:00:00Z"
    assert resolved.version == "2.0.0"
    assert requests[1].url.raw_path == (
        b"/v0.1/servers/io.example%2Foperations/versions/2.0.0"
    )
