"""Forward-migration regression for the MCP endpoint and lifecycle invariants."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

MIGRATION_TEST_URL = os.getenv("MCP_MIGRATION_TEST_DATABASE_URL")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


async def _execute(database_url: str, statement: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _scalar(database_url: str, statement: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement))).scalar_one()
    finally:
        await engine.dispose()


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def _upgrade(database_url: str, revision: str) -> None:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.mark.skipif(
    MIGRATION_TEST_URL is None,
    reason="MCP_MIGRATION_TEST_DATABASE_URL is not configured",
)
def test_a100_to_b200_to_c300_postgresql_upgrade() -> None:
    """Exercise migration blockers, backfill/cascade, and lifecycle constraints."""

    assert MIGRATION_TEST_URL is not None
    database_name = make_url(MIGRATION_TEST_URL).database or ""
    assert database_name.endswith("_mcp_migration_test"), (
        "migration regression requires a dedicated database whose name ends "
        "with _mcp_migration_test"
    )

    asyncio.run(_reset_schema(MIGRATION_TEST_URL))
    try:
        _upgrade(MIGRATION_TEST_URL, "a10047518e6a")
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                INSERT INTO gateway_mcp_servers (
                    id, workspace_id, scope_type, agent_id, target_id, target_type,
                    server_name, server_url, enabled, auth_type, public_headers,
                    credential_mode, credential_transitioning, provenance_type,
                    endpoint_configuration, revision, connection_status
                ) VALUES
                  ('11111111-1111-4111-8111-111111111111', 'ws-migration',
                   'target', NULL, 'cluster-a', 'kubernetes', 'remote',
                   'https://canonical.example/mcp', true, 'bearer', '{}',
                   'individual', false, 'manual', '{}', 1, 'connected'),
                  ('22222222-2222-4222-8222-222222222222', 'ws-migration',
                   'agent', 'agent-a', NULL, NULL, 'builtin-a',
                   'https://builtin-a.example/mcp', true, 'none', '{}',
                   'none', false, 'builtin', '{}', 1, 'connected'),
                  ('33333333-3333-4333-8333-333333333333', 'ws-migration',
                   'agent', 'agent-a', NULL, NULL, 'builtin-b',
                   'https://builtin-b.example/mcp', true, 'none', '{}',
                   'none', false, 'builtin', '{}', 1, 'connected')
                """,
            )
        )
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                INSERT INTO gateway_tools (
                    id, server_id, workspace_id, scope_type, target_id,
                    target_type, tool_name, mcp_server_url, enabled, input_schema,
                    artifact_policy, capability, review_state, risk_level,
                    auto_allowed, version, source, timeout_ms
                ) VALUES (
                    '44444444-4444-4444-8444-444444444444',
                    '11111111-1111-4111-8111-111111111111', 'ws-migration',
                    'target', 'cluster-a', 'kubernetes', 'list_items',
                    'https://stale.example/mcp', true, '{}', 'inline', 'read',
                    'allowed', 'low', true, '1', 'mcp', 1000
                )
                """,
            )
        )
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                INSERT INTO gateway_mcp_connections (
                    id, workspace_id, server_id, owner_type, owner_id, status,
                    verified_tool_names, oauth_scopes, oauth_refresh_capable
                ) VALUES (
                    '55555555-5555-4555-8555-555555555555', 'ws-migration',
                    '11111111-1111-4111-8111-111111111111', 'user', 'user-a',
                    'connected', '[]', '[]', false
                )
                """,
            )
        )

        with pytest.raises(RuntimeError, match="credential connections"):
            _upgrade(MIGRATION_TEST_URL, "b20058629f4b")
        assert asyncio.run(
            _scalar(MIGRATION_TEST_URL, "SELECT version_num FROM alembic_version")
        ) == "a10047518e6a"

        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                "DELETE FROM gateway_mcp_connections WHERE workspace_id='ws-migration'",
            )
        )
        with pytest.raises(RuntimeError, match="stale tool definitions"):
            _upgrade(MIGRATION_TEST_URL, "b20058629f4b")
        assert asyncio.run(
            _scalar(MIGRATION_TEST_URL, "SELECT version_num FROM alembic_version")
        ) == "a10047518e6a"
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                UPDATE gateway_mcp_servers
                   SET credential_transitioning=true
                 WHERE id='11111111-1111-4111-8111-111111111111'
                """,
            )
        )
        with pytest.raises(RuntimeError, match="stale tool definitions"):
            _upgrade(MIGRATION_TEST_URL, "b20058629f4b")
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                "DELETE FROM gateway_tools WHERE server_id="
                "'11111111-1111-4111-8111-111111111111'",
            )
        )
        _upgrade(MIGRATION_TEST_URL, "b20058629f4b")
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                "SELECT count(*) FROM gateway_tools WHERE tool_name='list_items'",
            )
        ) == 0
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                INSERT INTO gateway_tools (
                    id, server_id, workspace_id, scope_type, target_id,
                    target_type, tool_name, mcp_server_url, enabled, input_schema,
                    artifact_policy, capability, review_state, risk_level,
                    auto_allowed, version, source, timeout_ms
                ) VALUES (
                    '44444444-4444-4444-8444-444444444444',
                    '11111111-1111-4111-8111-111111111111', 'ws-migration',
                    'target', 'cluster-a', 'kubernetes', 'list_items',
                    'https://canonical.example/mcp', false, '{}', 'inline', 'read',
                    'pending', 'low', false, '1', 'mcp', 1000
                )
                """,
            )
        )
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                UPDATE gateway_mcp_servers
                   SET server_url='https://canonical-v2.example/mcp'
                 WHERE id='11111111-1111-4111-8111-111111111111'
                """,
            )
        )
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                "SELECT mcp_server_url FROM gateway_tools WHERE tool_name='list_items'",
            )
        ) == "https://canonical-v2.example/mcp"

        with pytest.raises(RuntimeError, match="Duplicate built-in MCP servers"):
            _upgrade(MIGRATION_TEST_URL, "c3006973a8d2")
        assert asyncio.run(
            _scalar(MIGRATION_TEST_URL, "SELECT version_num FROM alembic_version")
        ) == "b20058629f4b"
        asyncio.run(
            _execute(
                MIGRATION_TEST_URL,
                """
                DELETE FROM gateway_mcp_servers
                 WHERE id='33333333-3333-4333-8333-333333333333'
                """,
            )
        )
        _upgrade(MIGRATION_TEST_URL, "c3006973a8d2")
        assert asyncio.run(
            _scalar(MIGRATION_TEST_URL, "SELECT version_num FROM alembic_version")
        ) == "c3006973a8d2"
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                """
                SELECT credential_epoch
                  FROM gateway_mcp_servers
                 WHERE id='11111111-1111-4111-8111-111111111111'
                """,
            )
        ) == 1
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                "SELECT to_regclass('gateway_mcp_user_lifecycles')::text",
            )
        ) == "gateway_mcp_user_lifecycles"
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                "SELECT to_regclass('ix_gateway_secrets_tenant_scope_name')::text",
            )
        ) == "ix_gateway_secrets_tenant_scope_name"
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                """
                SELECT count(*) = 3
                  FROM information_schema.columns
                 WHERE table_name='gateway_catalog_sources'
                   AND column_name IN (
                       'authority_generation',
                       'credential_transitioning',
                       'previous_auth_secret_name'
                   )
                """,
            )
        ) is True
        assert asyncio.run(
            _scalar(
                MIGRATION_TEST_URL,
                """
                SELECT count(*) = 1
                  FROM information_schema.columns
                 WHERE table_name='gateway_catalog_bindings'
                   AND column_name='sync_generation'
                """,
            )
        ) is True

        with pytest.raises(IntegrityError):
            asyncio.run(
                _execute(
                    MIGRATION_TEST_URL,
                    """
                    INSERT INTO gateway_mcp_servers (
                        id, workspace_id, scope_type, agent_id, server_name,
                        server_url, enabled, auth_type, public_headers,
                        credential_mode, credential_transitioning,
                        provenance_type, endpoint_configuration, revision,
                        connection_status, credential_epoch
                    ) VALUES (
                        '66666666-6666-4666-8666-666666666666', 'ws-migration',
                        'agent', 'agent-a', 'builtin-c',
                        'https://builtin-c.example/mcp', true, 'none', '{}',
                        'none', false, 'builtin', '{}', 1, 'connected', 1
                    )
                    """,
                )
            )
    finally:
        asyncio.run(_reset_schema(MIGRATION_TEST_URL))
