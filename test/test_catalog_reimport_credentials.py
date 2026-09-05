import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import handlers_catalog_admin as handlers
from app.api.catalog_endpoint_configuration import ResolvedCatalogEndpoint
from app.api.handlers_catalog_admin import import_catalog_mcp_server
from app.catalog.schemas import CatalogMcpImportRequest


@pytest.fixture(autouse=True)
def catalog_import_lifecycle_guards(monkeypatch):
    @asynccontextmanager
    async def workspace_guard(_workspace_id):
        yield

    @asynccontextmanager
    async def server_guard(workspace_id, server_id, **_kwargs):
        current = await handlers.mcp_server_registry.get_server(
            workspace_id,
            "cluster-a",
            server_id,
            scope_type="target",
            target_type="kubernetes",
        )
        yield current

    monkeypatch.setattr(handlers, "guarded_workspace_mutation", workspace_guard)
    monkeypatch.setattr(handlers, "guarded_server_operation", server_guard)


def _undecorated_import_handler():
    handler = import_catalog_mcp_server
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__
    return handler


def _request(
    server_id: uuid.UUID,
    *,
    credential_mode: str = "workspace",
) -> CatalogMcpImportRequest:
    return CatalogMcpImportRequest(
        root={
            "workspace_id": "ws-1",
            "scope_type": "target",
            "target_id": "cluster-a",
            "target_type": "kubernetes",
            "artifact": {"artifact_id": "artifact-1"},
            "version": "2",
            "remote_endpoint": "https://mcp.example.test/mcp",
            "credential_mode": credential_mode,
            "reimport_server_id": str(server_id),
            "expected_revision": 4,
        }
    )


def _artifact(source_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_id=source_id,
        artifact_name="example-server",
        version="2",
        digest="sha256:new-artifact",
        title="Example server",
    )


def _server(source_id: uuid.UUID, server_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id,
        workspace_id="ws-1",
        provenance_type="catalog",
        catalog_source_id=source_id,
        catalog_artifact_name="example-server",
        catalog_digest="sha256:old-artifact",
        revision=4,
        server_url="https://mcp.example.test/mcp",
        auth_type="bearer_token",
        auth_header_name="Authorization",
        auth_header_prefix="Bearer",
        credential_mode="workspace",
        public_headers={},
        credential_epoch=7,
    )


async def _reimport(
    *,
    resolved_auth_type: str,
    credential_mode: str = "workspace",
    resolved_url: str = "https://mcp.example.test/mcp",
    discovery_error: str | None = None,
):
    source_id = uuid.uuid4()
    server_id = uuid.uuid4()
    artifact = _artifact(source_id)
    server = _server(source_id, server_id)
    transition = SimpleNamespace(id=server_id, revision=5)
    updated = SimpleNamespace(id=server_id, revision=6)
    reconciled = SimpleNamespace(id=server_id, revision=7)
    update_server = AsyncMock(side_effect=[transition, updated, reconciled])
    cleanup = AsyncMock()
    remove_tools = AsyncMock()
    resolved = ResolvedCatalogEndpoint(
        url=resolved_url,
        public_headers={},
        supported_credential_modes=(
            ("none",) if credential_mode == "none" else ("workspace", "individual")
        ),
        credential_mode=credential_mode,
        credential_header_name=(None if credential_mode == "none" else "Authorization"),
        credential_auth_type=resolved_auth_type,
        credential_auth_header_prefix="Bearer",
    )
    catalog_source = SimpleNamespace(
        id=source_id,
        authority_generation=1,
        enabled=True,
        credential_transitioning=False,
    )
    catalog_binding = SimpleNamespace(id=uuid.uuid4())
    with (
        patch(
            "app.api.handlers_catalog_admin.catalog_store.get_artifact",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin._load_catalog_import_authority",
            new=AsyncMock(
                return_value=(catalog_source, catalog_binding, ("authority",))
            ),
        ),
        patch(
            "app.api.handlers_catalog_admin._revalidate_catalog_import_authority",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin.resolve_catalog_endpoint",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.get_server",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.get_server_by_url",
            new=AsyncMock(return_value=server),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.update_server",
            new=update_server,
        ),
        patch(
            "app.api.handlers_catalog_admin.cleanup_server_connections",
            new=cleanup,
        ),
        patch(
            "app.api.handlers_catalog_admin._discover_server_tools",
            new=AsyncMock(return_value=([], discovery_error, "MCP_DISCOVERY_FAILED")),
        ),
        patch(
            "app.api.handlers_catalog_admin._apply_tools_for_server",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_catalog_admin.tool_registry.remove_server_tools_not_in",
            new=remove_tools,
        ),
        patch(
            "app.api.handlers_catalog_admin._record_discovery_status",
            new=AsyncMock(return_value=updated),
        ),
        patch(
            "app.api.handlers_catalog_admin._resolve_tools_for_server",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_catalog_admin._build_server_response",
            return_value=updated,
        ),
    ):
        try:
            result = await _undecorated_import_handler()(
                _request(server_id, credential_mode=credential_mode),
                _token_ok=None,
            )
            error = None
        except HTTPException as exc:
            result = None
            error = exc
    return result, update_server, cleanup, remove_tools, error


@pytest.mark.anyio
async def test_catalog_digest_only_reimport_preserves_credentials_and_epoch() -> None:
    _result, update_server, cleanup, remove_tools, error = await _reimport(
        resolved_auth_type="bearer_token"
    )

    assert error is None
    cleanup.assert_not_awaited()
    remove_tools.assert_not_awaited()
    update_server.assert_awaited_once()
    patch_fields = update_server.await_args.args[3]
    assert patch_fields["catalog_digest"] == "sha256:new-artifact"
    assert patch_fields["expected_revision"] == 4
    assert "credential_epoch" not in patch_fields
    assert "credential_transitioning" not in patch_fields


@pytest.mark.anyio
async def test_catalog_auth_change_drains_credentials_and_bumps_epoch() -> None:
    _result, update_server, cleanup, remove_tools, error = await _reimport(
        resolved_auth_type="custom_header"
    )

    assert error is None
    assert update_server.await_count == 2
    transition = update_server.await_args_list[0].args[3]
    assert transition["credential_transitioning"] is True
    assert transition["credential_epoch"] == 8
    cleanup.assert_awaited_once()
    assert any(call.kwargs.get("tool_names") == set() for call in remove_tools.await_args_list)
    final_patch = update_server.await_args_list[1].args[3]
    assert final_patch["expected_revision"] == 5
    assert final_patch["credential_transitioning"] is False


@pytest.mark.anyio
async def test_catalog_credential_free_endpoint_change_stays_fenced_on_discovery_failure() -> None:
    _result, update_server, cleanup, _remove_tools, error = await _reimport(
        resolved_auth_type="none",
        credential_mode="none",
        resolved_url="https://new-mcp.example.test/mcp",
        discovery_error="MCP server discovery failed.",
    )

    assert error is not None
    assert error.status_code == 503
    assert "remains fenced" in str(error.detail)
    cleanup.assert_awaited_once()
    assert update_server.await_count == 2
    assert update_server.await_args_list[0].args[3]["credential_transitioning"] is True
    final_patch = update_server.await_args_list[1].args[3]
    assert final_patch["server_url"] == "https://new-mcp.example.test/mcp"
    assert final_patch["credential_transitioning"] is True
    assert not any(
        call.args[3] == {"credential_transitioning": False}
        for call in update_server.await_args_list
    )


@pytest.mark.anyio
async def test_catalog_credential_free_endpoint_change_unfences_after_reconciliation() -> None:
    _result, update_server, cleanup, _remove_tools, error = await _reimport(
        resolved_auth_type="none",
        credential_mode="none",
        resolved_url="https://new-mcp.example.test/mcp",
    )

    assert error is None
    cleanup.assert_awaited_once()
    assert update_server.await_count == 3
    assert update_server.await_args_list[-1].args[3] == {"credential_transitioning": False}


@pytest.mark.anyio
async def test_catalog_discovery_failure_exact_retry_resumes_persisted_transition() -> None:
    source_id = uuid.uuid4()
    server_id = uuid.uuid4()
    artifact = _artifact(source_id)
    initial = SimpleNamespace(
        **{
            **_server(source_id, server_id).__dict__,
            "server_name": "Example server",
            "enabled": True,
            "catalog_version": "1",
            "endpoint_configuration": {},
            "credential_transitioning": False,
        }
    )
    transition = SimpleNamespace(**{**initial.__dict__, "revision": 5})
    desired = {
        **initial.__dict__,
        "server_url": "https://new-mcp.example.test/mcp",
        "auth_type": "none",
        "auth_header_name": None,
        "auth_header_prefix": None,
        "credential_mode": "none",
        "catalog_version": artifact.version,
        "catalog_digest": artifact.digest,
        "credential_transitioning": True,
        "revision": 6,
        "connection_status": "error",
        "last_discovery_at": None,
        "last_discovery_error": "MCP server discovery failed.",
    }
    failed = SimpleNamespace(**{**desired, "revision": 7})
    retry_updated = SimpleNamespace(**{**desired, "revision": 8})
    discovery_ok = SimpleNamespace(
        **{**retry_updated.__dict__, "revision": 9, "last_discovery_error": None}
    )
    reconciled = SimpleNamespace(
        **{**discovery_ok.__dict__, "revision": 10, "credential_transitioning": False}
    )
    update_server = AsyncMock(
        side_effect=[transition, SimpleNamespace(**desired), retry_updated, reconciled]
    )
    resolved = ResolvedCatalogEndpoint(
        url="https://new-mcp.example.test/mcp",
        public_headers={},
        supported_credential_modes=("none",),
        credential_mode="none",
        credential_header_name=None,
        credential_auth_type="none",
        credential_auth_header_prefix=None,
    )
    discover = AsyncMock(
        side_effect=[
            ([], "MCP server discovery failed.", "MCP_DISCOVERY_FAILED"),
            ([], None, None),
        ]
    )
    with (
        patch(
            "app.api.handlers_catalog_admin.catalog_store.get_artifact",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin._load_catalog_import_authority",
            new=AsyncMock(
                return_value=(
                    SimpleNamespace(
                        id=source_id,
                        authority_generation=1,
                        enabled=True,
                        credential_transitioning=False,
                    ),
                    SimpleNamespace(id=uuid.uuid4()),
                    ("authority",),
                )
            ),
        ),
        patch(
            "app.api.handlers_catalog_admin._revalidate_catalog_import_authority",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin.resolve_catalog_endpoint",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.get_server",
            new=AsyncMock(side_effect=[initial, failed]),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.get_server_by_url",
            new=AsyncMock(side_effect=[initial, failed]),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.update_server",
            new=update_server,
        ),
        patch(
            "app.api.handlers_catalog_admin.cleanup_server_connections",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.api.handlers_catalog_admin._discover_server_tools",
            new=discover,
        ),
        patch(
            "app.api.handlers_catalog_admin._apply_tools_for_server",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_catalog_admin.tool_registry.remove_server_tools_not_in",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_catalog_admin._record_discovery_status",
            new=AsyncMock(side_effect=[failed, discovery_ok]),
        ),
        patch(
            "app.api.handlers_catalog_admin._resolve_tools_for_server",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_catalog_admin._build_server_response",
            side_effect=lambda server, _tools: server,
        ),
    ):
        request = _request(server_id, credential_mode="none")
        with pytest.raises(HTTPException) as first_error:
            await _undecorated_import_handler()(request, _token_ok=None)
        result = await _undecorated_import_handler()(request, _token_ok=None)

    assert first_error.value.status_code == 503
    assert result is reconciled
    assert update_server.await_args_list[2].args[3]["expected_revision"] == 7
    assert update_server.await_args_list[-1].args[3] == {"credential_transitioning": False}


@pytest.mark.anyio
async def test_catalog_cleanup_failure_exact_retry_resumes_transition() -> None:
    source_id = uuid.uuid4()
    server_id = uuid.uuid4()
    artifact = _artifact(source_id)
    initial = SimpleNamespace(
        **{
            **_server(source_id, server_id).__dict__,
            "server_name": "Example server",
            "enabled": True,
            "catalog_version": "1",
            "endpoint_configuration": {},
            "credential_transitioning": False,
        }
    )
    transition = SimpleNamespace(
        **{**initial.__dict__, "revision": 5, "credential_transitioning": True}
    )
    updated = SimpleNamespace(
        **{
            **initial.__dict__,
            "revision": 6,
            "auth_type": "custom_header",
            "credential_transitioning": False,
        }
    )
    resolved = ResolvedCatalogEndpoint(
        url=initial.server_url,
        public_headers={},
        supported_credential_modes=("workspace", "individual"),
        credential_mode="workspace",
        credential_header_name="Authorization",
        credential_auth_type="custom_header",
        credential_auth_header_prefix="Bearer",
    )
    update_server = AsyncMock(side_effect=[transition, updated])
    cleanup = AsyncMock(side_effect=[RuntimeError("secret backend unavailable"), 1])
    with (
        patch(
            "app.api.handlers_catalog_admin.catalog_store.get_artifact",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin._load_catalog_import_authority",
            new=AsyncMock(
                return_value=(
                    SimpleNamespace(
                        id=source_id,
                        authority_generation=1,
                        enabled=True,
                        credential_transitioning=False,
                    ),
                    SimpleNamespace(id=uuid.uuid4()),
                    ("authority",),
                )
            ),
        ),
        patch(
            "app.api.handlers_catalog_admin._revalidate_catalog_import_authority",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin.resolve_catalog_endpoint",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.get_server",
            new=AsyncMock(side_effect=[initial, transition]),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.get_server_by_url",
            new=AsyncMock(side_effect=[initial, transition]),
        ),
        patch(
            "app.api.handlers_catalog_admin.mcp_server_registry.update_server",
            new=update_server,
        ),
        patch(
            "app.api.handlers_catalog_admin.cleanup_server_connections",
            new=cleanup,
        ),
        patch(
            "app.api.handlers_catalog_admin._apply_tools_for_server",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_catalog_admin.tool_registry.remove_server_tools_not_in",
            new=AsyncMock(),
        ),
        patch(
            "app.api.handlers_catalog_admin._record_discovery_status",
            new=AsyncMock(return_value=updated),
        ),
        patch(
            "app.api.handlers_catalog_admin._resolve_tools_for_server",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.handlers_catalog_admin._build_server_response",
            side_effect=lambda server, _tools: server,
        ),
    ):
        request = _request(server_id)
        with pytest.raises(HTTPException) as first_error:
            await _undecorated_import_handler()(request, _token_ok=None)
        result = await _undecorated_import_handler()(request, _token_ok=None)

    assert first_error.value.status_code == 503
    assert result is updated
    assert update_server.await_count == 2
    assert update_server.await_args_list[1].args[3]["expected_revision"] == 5
    assert cleanup.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("resolved_name", "resolved_version"),
    [("different-server", "2"), ("example-server", "3")],
)
async def test_on_demand_catalog_fetch_rejects_substituted_artifact_identity(
    resolved_name: str,
    resolved_version: str,
) -> None:
    source_id = uuid.uuid4()
    server_id = uuid.uuid4()
    artifact = _artifact(source_id)
    artifact.version = "1"
    source = SimpleNamespace(
        id=source_id,
        base_url="https://registry.example",
        enabled=True,
        credential_transitioning=False,
    )
    binding = SimpleNamespace(id=uuid.uuid4(), adapter_base_path="/v0.1")
    upsert = AsyncMock()

    class Adapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def fetch_artifact(self, *_args):
            return SimpleNamespace(name=resolved_name, version=resolved_version)

    with (
        patch(
            "app.api.handlers_catalog_admin.catalog_store.get_artifact",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin._load_catalog_import_authority",
            new=AsyncMock(return_value=(source, binding, ("authority",))),
        ),
        patch(
            "app.api.handlers_catalog_admin._source_headers",
            new=AsyncMock(return_value={}),
        ),
        patch("app.api.handlers_catalog_admin.McpRegistryV01Adapter", Adapter),
        patch(
            "app.api.handlers_catalog_admin.catalog_store.upsert_artifacts",
            new=upsert,
        ),
        pytest.raises(HTTPException) as raised,
    ):
        await _undecorated_import_handler()(
            _request(server_id),
            _token_ok=None,
        )

    assert raised.value.status_code == 502
    upsert.assert_not_awaited()


@pytest.mark.anyio
async def test_import_authority_revalidation_rejects_source_or_artifact_change() -> None:
    artifact = _artifact(uuid.uuid4())
    with (
        patch(
            "app.api.handlers_catalog_admin.catalog_store.get_artifact",
            new=AsyncMock(return_value=artifact),
        ),
        patch(
            "app.api.handlers_catalog_admin._load_catalog_import_authority",
            new=AsyncMock(return_value=(object(), object(), ("new-authority",))),
        ),
        pytest.raises(HTTPException) as raised,
    ):
        await handlers._revalidate_catalog_import_authority(
            "ws-1",
            str(artifact.id),
            ("old-authority",),
        )

    assert raised.value.status_code == 409
