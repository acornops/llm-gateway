import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.api import catalog_bootstrap
from app.api import handlers_catalog_sources as handlers
from app.catalog.schemas import CatalogSourceCreateRequest, CatalogSourcePatchRequest

GENERATED_SECRET = "catalog_source::aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def catalog_workspace_guard(monkeypatch):
    @asynccontextmanager
    async def allow(_workspace_id):
        yield

    async def active(_workspace_id):
        return None

    monkeypatch.setattr(handlers, "guarded_workspace_mutation", allow)
    monkeypatch.setattr(handlers, "assert_guarded_workspace_active", active)


def source_pair(
    *,
    enabled: bool = True,
    auth_type: str = "bearer_token",
    secret_name: str | None = GENERATED_SECRET,
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
        credential_transitioning=False,
        previous_auth_secret_name=None,
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
        self.values: dict[str, str] = {GENERATED_SECRET: "stored-credential"}

    async def get_secret(self, name, _scope):
        self.reads += 1
        return self.values[name]

    async def put_secret(self, name, value, _scope):
        self.put.append((name, value))
        self.values[name] = value

    async def delete_secret(self, name, _scope):
        self.deleted.append(name)
        self.values.pop(name, None)


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

    update_options = []

    async def update_source(_workspace_id, _source_id, changes, **options):
        update_options.append(options)
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
    assert secrets.deleted == [GENERATED_SECRET]
    assert update_options == [
        {"clear_artifacts": True},
        {},
    ]


@pytest.mark.anyio
async def test_credential_delete_failure_is_fenced_and_exact_retry_resumes(
    monkeypatch,
) -> None:
    source, binding = source_pair()
    secrets = FakeSecretStore()
    delete_attempts = 0
    synchronizations = []

    async def get_source_binding(*_args):
        return source, binding

    async def update_source(_workspace_id, _source_id, changes, **_options):
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    async def delete_secret(name, _scope):
        nonlocal delete_attempts
        delete_attempts += 1
        if delete_attempts == 1:
            raise RuntimeError("vault unavailable")
        secrets.deleted.append(name)

    class ProbeAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def probe(self):
            return None

    async def sync_source(workspace_id, source_id, *, incremental):
        synchronizations.append((workspace_id, source_id, incremental))
        return 0

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "update_source", update_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)
    monkeypatch.setattr(secrets, "delete_secret", delete_secret)
    monkeypatch.setattr(handlers, "McpRegistryV01Adapter", ProbeAdapter)
    monkeypatch.setattr(handlers, "sync_source", sync_source)

    request = CatalogSourcePatchRequest(auth={"type": "bearer_token", "credential": "replacement"})
    with pytest.raises(HTTPException) as raised:
        await handlers.update_catalog_source(
            request,
            source_id=str(source.id),
            workspace_id="workspace-a",
            _token_ok=None,
        )

    assert raised.value.status_code == 503
    assert source.credential_transitioning is True
    assert source.previous_auth_secret_name == GENERATED_SECRET
    assert source.auth_secret_name != GENERATED_SECRET
    assert len(secrets.put) == 1

    response = await handlers.update_catalog_source(
        request,
        source_id=str(source.id),
        workspace_id="workspace-a",
        _token_ok=None,
    )

    assert response.credential_configured is True
    assert source.credential_transitioning is False
    assert source.previous_auth_secret_name is None
    assert secrets.deleted == [GENERATED_SECRET]
    assert len(secrets.put) == 1
    assert synchronizations == [("workspace-a", str(source.id), False)]


@pytest.mark.anyio
async def test_different_patch_during_transition_recovers_then_requires_retry(
    monkeypatch,
) -> None:
    source, binding = source_pair()
    source.credential_transitioning = True
    source.previous_auth_secret_name = GENERATED_SECRET
    source.auth_secret_name = "catalog_source::bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    secrets = FakeSecretStore()
    secrets.values[source.auth_secret_name] = "replacement"

    async def get_source_binding(*_args):
        return source, binding

    async def update_source(_workspace_id, _source_id, changes, **_options):
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "update_source", update_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)

    with pytest.raises(HTTPException) as raised:
        await handlers.update_catalog_source(
            CatalogSourcePatchRequest(auth={"type": "none"}),
            source_id=str(source.id),
            workspace_id="workspace-a",
            _token_ok=None,
        )

    assert raised.value.status_code == 409
    assert source.credential_transitioning is False
    assert source.previous_auth_secret_name is None
    assert source.auth_type == "bearer_token"
    assert source.auth_secret_name != GENERATED_SECRET
    assert secrets.deleted == [GENERATED_SECRET]


@pytest.mark.anyio
async def test_transition_retry_compares_non_ascii_credential_as_utf8(
    monkeypatch,
) -> None:
    source, _binding = source_pair()
    source.credential_transitioning = True
    secrets = FakeSecretStore()
    secrets.values[GENERATED_SECRET] = "秘密-token"
    monkeypatch.setattr(handlers, "secret_store", secrets)

    matches = await handlers._patch_matches_persisted_source(
        CatalogSourcePatchRequest(
            auth={"type": "bearer_token", "credential": "秘密-token"}
        ),
        source,
        "workspace-a",
    )

    assert matches is True


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
async def test_duplicate_create_never_writes_new_credential(monkeypatch) -> None:
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
    monkeypatch.setattr(
        handlers.catalog_store,
        "list_sources",
        AsyncMock(return_value=[]),
    )

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
    assert secrets.put == []
    assert secrets.deleted == []


@pytest.mark.anyio
async def test_recreating_a_renamed_display_name_uses_a_distinct_secret_identity(
    monkeypatch,
) -> None:
    secrets = FakeSecretStore()
    created_source_ids = []
    created_sources = {}

    class ProbeAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def probe(self):
            return None

    async def create_source(**kwargs):
        source, binding = source_pair(secret_name=kwargs["auth_secret_name"])
        source.id = kwargs["source_id"]
        source.display_name = kwargs["display_name"]
        source.base_url = kwargs["base_url"]
        source.enabled = kwargs["enabled"]
        source.credential_transitioning = kwargs["credential_transitioning"]
        created_source_ids.append(source.id)
        created_sources[str(source.id)] = (source, binding)
        return source, binding

    async def update_source(_workspace_id, source_id, changes, **_options):
        source, binding = created_sources[source_id]
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    monkeypatch.setattr(handlers, "secret_store", secrets)
    monkeypatch.setattr(handlers, "McpRegistryV01Adapter", ProbeAdapter)
    monkeypatch.setattr(handlers.catalog_store, "create_source", create_source)
    monkeypatch.setattr(handlers.catalog_store, "update_source", update_source)
    monkeypatch.setattr(
        handlers.catalog_store,
        "list_sources",
        AsyncMock(return_value=[]),
    )

    for credential in ("credential-a", "credential-b"):
        await handlers.create_catalog_source(
            CatalogSourceCreateRequest(
                workspace_id="workspace-a",
                display_name="Reusable name",
                base_url="https://registry.example",
                auth_type="bearer_token",
                auth_secret_value=credential,
                enabled=False,
            ),
            _token_ok=None,
        )

    assert len(set(created_source_ids)) == 2
    assert len({name for name, _value in secrets.put}) == 2
    for source_id, (secret_name, _value) in zip(
        created_source_ids,
        secrets.put,
        strict=True,
    ):
        assert secret_name == f"catalog_source::{source_id}"


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

    assert set(secrets.deleted) == handlers._catalog_source_owned_secret_names(source.id) | {
        GENERATED_SECRET
    }
    assert deleted_sources == [("workspace-a", str(source.id))]


@pytest.mark.anyio
async def test_delete_preserves_external_secret_reference(monkeypatch) -> None:
    source, binding = source_pair(
        secret_name="shared-provider-credential",
    )
    secrets = FakeSecretStore()

    async def get_source_binding(*_args):
        return source, binding

    async def delete_source(_workspace_id, _source_id):
        return True

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "delete_source", delete_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)

    await handlers.delete_catalog_source(
        source_id=str(source.id), workspace_id="workspace-a", _token_ok=None
    )

    assert set(secrets.deleted) == handlers._catalog_source_owned_secret_names(source.id)


@pytest.mark.anyio
async def test_delete_cleans_unreferenced_replacement_slots_after_patch_crash(
    monkeypatch,
) -> None:
    source, binding = source_pair(secret_name="shared-provider-credential")
    secrets = FakeSecretStore()
    owned_names = handlers._catalog_source_owned_secret_names(source.id)
    for name in owned_names:
        secrets.values[name] = f"orphaned-{name}"
    secrets.values["shared-provider-credential"] = "provider-owned"

    async def get_source_binding(*_args):
        return source, binding

    async def delete_source(_workspace_id, _source_id):
        return True

    monkeypatch.setattr(handlers.catalog_store, "get_source_binding", get_source_binding)
    monkeypatch.setattr(handlers.catalog_store, "delete_source", delete_source)
    monkeypatch.setattr(handlers, "secret_store", secrets)

    await handlers.delete_catalog_source(
        source_id=str(source.id), workspace_id="workspace-a", _token_ok=None
    )

    assert set(secrets.deleted) == owned_names
    assert "shared-provider-credential" in secrets.values
    assert owned_names.isdisjoint(secrets.values)


@pytest.mark.anyio
async def test_bootstrap_name_collision_never_takes_over_workspace_source(
    monkeypatch,
) -> None:
    source, binding = source_pair(
        secret_name="provider-managed-secret",
        management_mode="workspace",
    )
    original = vars(source).copy()
    secrets = FakeSecretStore()
    secrets.values["provider-managed-secret"] = "shared-provider-value"

    @asynccontextmanager
    async def allow(_workspace_id):
        yield

    create_source = AsyncMock()
    update_source = AsyncMock()
    monkeypatch.setattr(catalog_bootstrap, "guarded_workspace_mutation", allow)
    monkeypatch.setattr(
        catalog_bootstrap.catalog_store,
        "list_sources",
        AsyncMock(return_value=[(source, binding)]),
    )
    monkeypatch.setattr(catalog_bootstrap.catalog_store, "create_source", create_source)
    monkeypatch.setattr(catalog_bootstrap.catalog_store, "update_source", update_source)
    monkeypatch.setattr(catalog_bootstrap, "secret_store", secrets)

    generated_secret = "catalog_bootstrap::0123456789abcdef01234567"
    result = await catalog_bootstrap._persist_desired_source(
        "workspace-a",
        CatalogSourceCreateRequest(
            workspace_id="workspace-a",
            display_name=source.display_name,
            base_url="https://bootstrap.example",
            auth_type="bearer_token",
            auth_secret_name=generated_secret,
            enabled=True,
            management_mode="bootstrap",
        ),
        "bootstrap-credential",
        generated_secret,
    )

    assert result is None
    assert vars(source) == original
    assert secrets.values["provider-managed-secret"] == "shared-provider-value"
    assert secrets.put == []
    assert secrets.deleted == []
    create_source.assert_not_awaited()
    update_source.assert_not_awaited()


@pytest.mark.anyio
async def test_bootstrap_transition_recovery_applies_changed_desired_configuration(
    monkeypatch,
) -> None:
    source, binding = source_pair(management_mode="bootstrap")
    source.credential_transitioning = True
    source.previous_auth_secret_name = "catalog_bootstrap::aaaaaaaaaaaaaaaaaaaaaaaa"
    source.auth_secret_name = "catalog_bootstrap::bbbbbbbbbbbbbbbbbbbbbbbb"
    secrets = FakeSecretStore()
    secrets.values[source.previous_auth_secret_name] = "previous"
    secrets.values[source.auth_secret_name] = "current"

    @asynccontextmanager
    async def allow(_workspace_id):
        yield

    async def update_source(_workspace_id, _source_id, changes, **_options):
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    monkeypatch.setattr(catalog_bootstrap, "guarded_workspace_mutation", allow)
    monkeypatch.setattr(
        catalog_bootstrap.catalog_store,
        "list_sources",
        AsyncMock(return_value=[(source, binding)]),
    )
    monkeypatch.setattr(catalog_bootstrap.catalog_store, "update_source", update_source)
    monkeypatch.setattr(catalog_bootstrap, "secret_store", secrets)

    desired_secret = "catalog_bootstrap::cccccccccccccccccccccccc"
    result = await catalog_bootstrap._persist_desired_source(
        "workspace-a",
        CatalogSourceCreateRequest(
            workspace_id="workspace-a",
            display_name=source.display_name,
            base_url="https://new-bootstrap.example",
            auth_type="bearer_token",
            auth_secret_name=desired_secret,
            enabled=True,
            management_mode="bootstrap",
        ),
        "new-credential",
        desired_secret,
    )

    assert result is not None
    assert source.base_url == "https://new-bootstrap.example"
    assert source.auth_secret_name == desired_secret
    assert source.credential_transitioning is False
    assert source.previous_auth_secret_name is None
    assert secrets.values[desired_secret] == "new-credential"
    assert "catalog_bootstrap::aaaaaaaaaaaaaaaaaaaaaaaa" in secrets.deleted
    assert "catalog_bootstrap::bbbbbbbbbbbbbbbbbbbbbbbb" in secrets.deleted


@pytest.mark.anyio
async def test_bootstrap_create_persists_fenced_cursor_before_secret_and_recovers(
    monkeypatch,
) -> None:
    source, binding = source_pair(
        secret_name="catalog_bootstrap::cccccccccccccccccccccccc",
        management_mode="bootstrap",
    )
    source.credential_transitioning = True
    secrets = FakeSecretStore()
    events: list[str] = []
    list_sources = AsyncMock(side_effect=[[], [(source, binding)]])

    @asynccontextmanager
    async def allow(_workspace_id):
        yield

    async def create_source(**kwargs):
        events.append("cursor")
        assert kwargs["credential_transitioning"] is True
        return source, binding

    async def update_source(_workspace_id, _source_id, changes, **_options):
        events.append(
            "finalize" if changes.get("credential_transitioning") is False else "stage"
        )
        for key, value in changes.items():
            setattr(source, key, value)
        return source, binding

    original_put = secrets.put_secret
    put_attempts = 0

    async def put_secret(name, value, scope):
        nonlocal put_attempts
        put_attempts += 1
        events.append("put")
        if put_attempts == 1:
            raise RuntimeError("vault unavailable")
        await original_put(name, value, scope)

    monkeypatch.setattr(catalog_bootstrap, "guarded_workspace_mutation", allow)
    monkeypatch.setattr(catalog_bootstrap.catalog_store, "list_sources", list_sources)
    monkeypatch.setattr(catalog_bootstrap.catalog_store, "create_source", create_source)
    monkeypatch.setattr(catalog_bootstrap.catalog_store, "update_source", update_source)
    monkeypatch.setattr(secrets, "put_secret", put_secret)
    monkeypatch.setattr(catalog_bootstrap, "secret_store", secrets)
    request = CatalogSourceCreateRequest(
        workspace_id="workspace-a",
        display_name=source.display_name,
        base_url=source.base_url,
        auth_type="bearer_token",
        auth_secret_name=source.auth_secret_name,
        enabled=True,
        management_mode="bootstrap",
    )

    with pytest.raises(RuntimeError, match="vault unavailable"):
        await catalog_bootstrap._persist_desired_source(
            "workspace-a",
            request,
            "bootstrap-credential",
            source.auth_secret_name,
        )
    assert events == ["cursor", "put"]
    assert source.credential_transitioning is True

    recovered = await catalog_bootstrap._persist_desired_source(
        "workspace-a",
        request,
        "bootstrap-credential",
        source.auth_secret_name,
    )
    assert recovered is not None
    assert events == ["cursor", "put", "stage", "put", "finalize"]
    assert source.credential_transitioning is False
    assert secrets.values[source.auth_secret_name] == "bootstrap-credential"
