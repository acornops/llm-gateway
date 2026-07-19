import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.api import handlers_catalog_sources as handlers
from app.catalog.schemas import CatalogSourceCreateRequest, CatalogSourcePatchRequest


def source_pair(
    *,
    enabled: bool = True,
    auth_type: str = "bearer_token",
    secret_name: str | None = "catalog_source::old",
    management_mode: str = "workspace",
):
    source_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        workspace_id="workspace-a",
        display_name="Internal registry",
        base_url="https://registry.example",
        auth_type=auth_type,
        auth_secret_name=secret_name,
        auth_header_name=None,
        network_route="direct",
        enabled=enabled,
        management_mode=management_mode,
        created_at=None,
        updated_at=None,
    )
    binding = SimpleNamespace(
        id=uuid.uuid4(),
        artifact_kind="mcp_server",
        adapter_type="mcp_registry_v0_1",
        adapter_base_path="/v0.1",
        sync_status="ready",
        last_sync_at=None,
        last_sync_error=None,
    )
    return source, binding


class FakeSecretStore:
    def __init__(self) -> None:
        self.put: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.reads = 0

    async def get_secret(self, name, _scope):
        self.reads += 1
        return "stored-credential"

    async def put_secret(self, name, value, _scope):
        self.put.append((name, value))

    async def delete_secret(self, name, _scope):
        self.deleted.append(name)


@pytest.mark.anyio
async def test_disable_preserves_credential_without_reading_or_probing(
    monkeypatch,
) -> None:
    source, binding = source_pair()
    secrets = FakeSecretStore()
    update_calls = []

    async def get_source_binding(*_args):
        return source, binding

    async def update_source(_workspace_id, _source_id, changes, **options):
        update_calls.append((changes, options))
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    class UnexpectedProbe:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("disabling must not probe the registry")

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "update_source", update_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)
    monkeypatch.setattr(handlers, "McpRegistryV01Adapter", UnexpectedProbe)

    response = await handlers.update_catalog_source(
        CatalogSourcePatchRequest(enabled=False),
        source_id=str(source.id),
        workspace_id="workspace-a",
        _token_ok=None,
    )

    assert response.enabled is False
    assert response.auth_type == "bearer_token"
    assert response.credential_configured is True
    assert secrets.reads == 0
    assert update_calls == [({"enabled": False}, {"clear_artifacts": False})]


@pytest.mark.anyio
async def test_clear_auth_probes_then_full_syncs_and_removes_old_secret(
    monkeypatch,
) -> None:
    source, binding = source_pair()
    secrets = FakeSecretStore()
    probes = []
    synchronizations = []

    async def get_source_binding(*_args):
        return source, binding

    async def update_source(_workspace_id, _source_id, changes, **options):
        assert options == {"clear_artifacts": True}
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    class ProbeAdapter:
        def __init__(self, base_url, *, base_path, headers):
            probes.append((base_url, base_path, headers))

        async def probe(self):
            return None

    async def sync_source(workspace_id, source_id, *, incremental):
        synchronizations.append((workspace_id, source_id, incremental))
        return 0

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "update_source", update_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)
    monkeypatch.setattr(handlers, "McpRegistryV01Adapter", ProbeAdapter)
    monkeypatch.setattr(handlers, "sync_source", sync_source)

    response = await handlers.update_catalog_source(
        CatalogSourcePatchRequest(auth={"type": "none"}),
        source_id=str(source.id),
        workspace_id="workspace-a",
        _token_ok=None,
    )

    assert response.auth_type == "none"
    assert response.credential_configured is False
    assert probes == [("https://registry.example", "/v0.1", {})]
    assert synchronizations == [("workspace-a", str(source.id), False)]
    assert secrets.deleted == ["catalog_source::old"]


@pytest.mark.anyio
async def test_bootstrap_source_rejects_configuration_mutation(monkeypatch) -> None:
    source, binding = source_pair(management_mode="bootstrap")

    async def get_source_binding(*_args):
        return source, binding

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)

    with pytest.raises(HTTPException) as raised:
        await handlers.update_catalog_source(
            CatalogSourcePatchRequest(enabled=False),
            source_id=str(source.id),
            workspace_id="workspace-a",
            _token_ok=None,
        )

    assert raised.value.status_code == 409


@pytest.mark.anyio
async def test_duplicate_create_cleans_new_write_only_credential(monkeypatch) -> None:
    secrets = FakeSecretStore()

    class ProbeAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def probe(self):
            return None

    async def create_source(**_kwargs):
        raise IntegrityError("insert", {}, Exception("duplicate"))

    monkeypatch.setattr(handlers, "secret_store", secrets)
    monkeypatch.setattr(handlers, "McpRegistryV01Adapter", ProbeAdapter)
    monkeypatch.setattr(handlers.catalog_store, "create_source", create_source)

    request = CatalogSourceCreateRequest(
        workspace_id="workspace-a",
        display_name="Internal registry",
        base_url="https://registry.example",
        auth_type="bearer_token",
        auth_secret_value="write-only",
    )
    with pytest.raises(HTTPException) as raised:
        await handlers.create_catalog_source(request, _token_ok=None)

    assert raised.value.status_code == 409
    assert secrets.put[0][1] == "write-only"
    assert secrets.deleted == [secrets.put[0][0]]


@pytest.mark.anyio
async def test_delete_removes_source_credential_and_cache_record(monkeypatch) -> None:
    source, binding = source_pair()
    secrets = FakeSecretStore()
    deleted_sources = []

    async def get_source_binding(*_args):
        return source, binding

    async def delete_source(workspace_id, source_id):
        deleted_sources.append((workspace_id, source_id))
        return True

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "delete_source", delete_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)

    await handlers.delete_catalog_source(
        source_id=str(source.id), workspace_id="workspace-a", _token_ok=None
    )

    assert secrets.deleted == ["catalog_source::old"]
    assert deleted_sources == [("workspace-a", str(source.id))]
