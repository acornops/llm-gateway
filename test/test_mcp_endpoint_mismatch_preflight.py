from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.scripts.mcp_endpoint_mismatch_preflight import (
    DuplicateBuiltinDestination,
    MismatchedInstallation,
    _cleanup_connections,
    _cleanup_legacy_installation,
    _dedupe_builtin_destinations,
    _load_duplicate_builtin_destinations,
    _run,
)


@pytest.mark.anyio
async def test_cleanup_retries_every_mismatched_installation_including_zero_rows() -> None:
    cleanup = AsyncMock(side_effect=[2, 0, 1])
    installations = [
        MismatchedInstallation("workspace-a", "server-a", 2),
        MismatchedInstallation("workspace-a", "server-b", 0),
        MismatchedInstallation("workspace-b", "server-c", 1),
    ]

    cleaned = await _cleanup_connections(installations, cleanup)

    assert cleaned == 3
    assert cleanup.await_args_list[0].args == ("workspace-a", "server-a")
    assert cleanup.await_args_list[1].args == ("workspace-a", "server-b")
    assert cleanup.await_args_list[2].args == ("workspace-b", "server-c")
    assert cleanup.await_count == 3


@pytest.mark.anyio
@pytest.mark.parametrize("oauth_issuer", [None, "https://issuer.example.test"])
async def test_cleanup_uses_only_columns_available_before_c300(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    oauth_issuer: str | None,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'legacy-endpoint-cleanup.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_mcp_servers (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        provenance_type VARCHAR NOT NULL,
                        credential_transitioning BOOLEAN NOT NULL,
                        revision INTEGER NOT NULL,
                        connection_status VARCHAR NOT NULL,
                        last_discovery_at DATETIME,
                        last_discovery_error VARCHAR
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_tools (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        server_id VARCHAR NOT NULL,
                        enabled BOOLEAN,
                        review_state VARCHAR,
                        auto_allowed BOOLEAN
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO gateway_tools VALUES (
                        'tool-a', 'workspace-a', 'server-a', TRUE, 'approved', TRUE
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO gateway_mcp_servers VALUES (
                        'server-a', 'workspace-a', 'manual', FALSE, 1,
                        'ok', NULL, NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_mcp_connections (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        server_id VARCHAR NOT NULL,
                        owner_type VARCHAR NOT NULL,
                        owner_id VARCHAR NOT NULL,
                        oauth_issuer VARCHAR,
                        oauth_resource VARCHAR,
                        oauth_client_id VARCHAR,
                        oauth_endpoint_snapshot JSON
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO gateway_mcp_connections (
                        id, workspace_id, server_id, owner_type, owner_id, oauth_issuer
                    ) VALUES (
                        'connection-1', 'workspace-a', 'server-a', 'user', 'user-a',
                        :oauth_issuer
                    )
                    """
                ),
                {"oauth_issuer": oauth_issuer},
            )

        delete_secret = AsyncMock()
        delete_tokens = AsyncMock()
        revoke = AsyncMock()
        delete_connection_flow = AsyncMock()
        delete_server_flow = AsyncMock()
        delete_registration = AsyncMock()
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.secret_store.delete_secret",
            delete_secret,
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_flow_store.delete_for_connection",
            delete_connection_flow,
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_flow_store.delete_for_server",
            delete_server_flow,
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_registration_store.delete_for_server",
            delete_registration,
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_token_service.delete_tokens",
            delete_tokens,
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_token_service.revoke",
            revoke,
        )
        purge = AsyncMock(side_effect=[RuntimeError("secret purge failed"), 0])
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.secret_store.purge_mcp_secrets",
            purge,
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.secret_store.count_mcp_secrets",
            AsyncMock(return_value=0),
        )

        with pytest.raises(RuntimeError, match="secret purge failed"):
            await _cleanup_legacy_installation(
                "workspace-a",
                "server-a",
                engine=engine,
            )
        async with engine.connect() as connection:
            retry_cursor_count = (
                await connection.execute(text("SELECT COUNT(*) FROM gateway_tools"))
            ).scalar_one()
        assert retry_cursor_count == 1
        cleaned = await _cleanup_legacy_installation(
            "workspace-a",
            "server-a",
            engine=engine,
        )

        assert cleaned == 0
        delete_secret.assert_awaited_once()
        delete_tokens.assert_awaited_once_with(
            "workspace-a", "server-a", "user-a"
        )
        if oauth_issuer is None:
            revoke.assert_not_awaited()
        else:
            revoke.assert_awaited_once()
        delete_connection_flow.assert_awaited_once_with(
            "workspace-a", "server-a", "user-a"
        )
        assert delete_server_flow.await_count == 2
        assert delete_registration.await_count == 2
        async with engine.connect() as connection:
            remaining = (
                await connection.execute(
                    text("SELECT COUNT(*) FROM gateway_mcp_connections")
                )
            ).scalar_one()
        assert remaining == 0
        async with engine.connect() as connection:
            fenced = (
                await connection.execute(
                    text(
                        """
                        SELECT credential_transitioning, revision, connection_status
                          FROM gateway_mcp_servers
                         WHERE id = 'server-a'
                        """
                    )
                )
            ).one()
        assert tuple(fenced) == (True, 2, "error")
        async with engine.connect() as connection:
            tool_count = (
                await connection.execute(text("SELECT COUNT(*) FROM gateway_tools"))
            ).scalar_one()
        assert tool_count == 0
        assert purge.await_count == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_cleanup_fences_credential_free_mismatch_before_b200(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'credential-free-endpoint-cleanup.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_mcp_servers (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        provenance_type VARCHAR NOT NULL,
                        credential_transitioning BOOLEAN NOT NULL,
                        revision INTEGER NOT NULL,
                        connection_status VARCHAR NOT NULL,
                        last_discovery_at DATETIME,
                        last_discovery_error VARCHAR
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_tools (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        server_id VARCHAR NOT NULL,
                        enabled BOOLEAN,
                        review_state VARCHAR,
                        auto_allowed BOOLEAN
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_mcp_connections (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        server_id VARCHAR NOT NULL,
                        owner_type VARCHAR NOT NULL,
                        owner_id VARCHAR NOT NULL,
                        oauth_issuer VARCHAR,
                        oauth_resource VARCHAR,
                        oauth_client_id VARCHAR,
                        oauth_endpoint_snapshot JSON
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO gateway_tools VALUES (
                        'tool-a', 'workspace-a', 'server-a', TRUE, 'approved', TRUE
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO gateway_mcp_servers VALUES (
                        'server-a', 'workspace-a', 'manual', FALSE, 3,
                        'ok', NULL, NULL
                    )
                    """
                )
            )

        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_flow_store.delete_for_server",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.oauth_registration_store.delete_for_server",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.secret_store.purge_mcp_secrets",
            AsyncMock(return_value=0),
        )
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight.secret_store.count_mcp_secrets",
            AsyncMock(return_value=0),
        )

        cleaned = await _cleanup_legacy_installation(
            "workspace-a",
            "server-a",
            engine=engine,
        )

        assert cleaned == 0
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT credential_transitioning, revision, connection_status,
                               last_discovery_error
                          FROM gateway_mcp_servers
                         WHERE id = 'server-a'
                        """
                    )
                )
            ).one()
        assert tuple(row) == (
            True,
            4,
            "error",
            "Endpoint canonicalization requires authoritative rediscovery.",
        )
        async with engine.connect() as connection:
            tool_count = (
                await connection.execute(text("SELECT COUNT(*) FROM gateway_tools"))
            ).scalar_one()
        assert tool_count == 0
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_legacy_agent_duplicate_preflight_requires_explicit_canonical_id(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'legacy-builtin-duplicates.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE gateway_mcp_servers (
                        id VARCHAR PRIMARY KEY,
                        workspace_id VARCHAR NOT NULL,
                        scope_type VARCHAR NOT NULL,
                        agent_id VARCHAR,
                        target_id VARCHAR,
                        target_type VARCHAR,
                        provenance_type VARCHAR NOT NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO gateway_mcp_servers VALUES
                      ('server-a', 'workspace-a', 'agent', 'agent-a', NULL, NULL, 'builtin'),
                      ('server-b', 'workspace-a', 'agent', 'agent-a', NULL, NULL, 'builtin'),
                      ('server-c', 'workspace-a', 'agent', 'agent-b', NULL, NULL, 'builtin')
                    """
                )
            )

        duplicates = await _load_duplicate_builtin_destinations(engine=engine)
        assert len(duplicates) == 1
        duplicate = duplicates[0]
        assert (
            duplicate.workspace_id,
            duplicate.scope_type,
            duplicate.destination_id,
            duplicate.server_ids,
        ) == (
            "workspace-a",
            "agent",
            "agent-a",
            ("server-a", "server-b"),
        )

        cleanup = AsyncMock(return_value=0)
        monkeypatch.setattr(
            "app.scripts.mcp_endpoint_mismatch_preflight._cleanup_legacy_installation",
            cleanup,
        )
        deleted = await _dedupe_builtin_destinations(
            duplicates,
            ["server-a"],
            engine=engine,
        )

        assert deleted == 1
        cleanup.assert_awaited_once_with(
            "workspace-a",
            "server-b",
            engine=engine,
        )
        async with engine.connect() as connection:
            remaining = list(
                (
                    await connection.execute(
                        text("SELECT id FROM gateway_mcp_servers ORDER BY id")
                    )
                ).scalars()
            )
        assert remaining == ["server-a", "server-c"]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_apply_cleanup_fails_when_any_duplicate_builtin_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = DuplicateBuiltinDestination(
        workspace_id="workspace-a",
        scope_type="agent",
        destination_id="agent-a",
        target_type=None,
        server_ids=("server-a", "server-b"),
    )
    monkeypatch.setattr(
        "app.scripts.mcp_endpoint_mismatch_preflight._load_mismatched_installations",
        AsyncMock(side_effect=[[], []]),
    )
    monkeypatch.setattr(
        "app.scripts.mcp_endpoint_mismatch_preflight._load_duplicate_builtin_destinations",
        AsyncMock(side_effect=[[duplicate], [duplicate]]),
    )
    monkeypatch.setattr(
        "app.scripts.mcp_endpoint_mismatch_preflight._cleanup_connections",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.scripts.mcp_endpoint_mismatch_preflight._dedupe_builtin_destinations",
        AsyncMock(return_value=0),
    )

    result = await _run(
        apply_cleanup=True,
        fail_on_active_connections=False,
        fail_on_duplicate_builtins=False,
        canonical_builtin_server_ids=[],
    )

    assert result == 2


@pytest.mark.anyio
async def test_report_acceptance_fails_for_fenced_credential_free_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.scripts.mcp_endpoint_mismatch_preflight._load_mismatched_installations",
        AsyncMock(
            return_value=[
                MismatchedInstallation(
                    "workspace-a",
                    "server-a",
                    0,
                    provenance_type="manual",
                    credential_transitioning=True,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "app.scripts.mcp_endpoint_mismatch_preflight._load_duplicate_builtin_destinations",
        AsyncMock(return_value=[]),
    )

    result = await _run(
        apply_cleanup=False,
        fail_on_active_connections=True,
        fail_on_duplicate_builtins=True,
        canonical_builtin_server_ids=[],
    )

    assert result == 2
