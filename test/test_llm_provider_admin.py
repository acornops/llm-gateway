from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.secrets.errors import SecretNotFoundError

ADMIN_HEADERS = {"Authorization": "Bearer dev_orchestrator_token"}


def _providers_by_name(response_json: dict) -> dict[str, dict]:
    return {provider["provider"]: provider for provider in response_json["providers"]}


@pytest.mark.anyio
async def test_provider_credential_admin_status_write_and_delete():
    with (
        patch(
            "app.api.handlers_llm_provider_admin.secret_store.get_secret",
            new_callable=AsyncMock,
            side_effect=SecretNotFoundError("missing"),
        ) as mock_get_secret,
        patch(
            "app.api.handlers_llm_provider_admin.secret_store.put_secret",
            new_callable=AsyncMock,
        ) as mock_put_secret,
        patch(
            "app.api.handlers_llm_provider_admin.secret_store.delete_secret",
            new_callable=AsyncMock,
        ) as mock_delete_secret,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            status_response = await ac.get(
                "/api/v1/internal/llm/provider-credentials?workspace_id=ws-1",
                headers=ADMIN_HEADERS,
            )
            put_response = await ac.put(
                "/api/v1/internal/llm/provider-credentials/openai",
                headers=ADMIN_HEADERS,
                json={"workspace_id": "ws-1", "api_key": "sk-test"},
            )
            delete_response = await ac.delete(
                "/api/v1/internal/llm/provider-credentials/openai?workspace_id=ws-1",
                headers=ADMIN_HEADERS,
            )

    assert status_response.status_code == 200
    status_providers = status_response.json()["providers"]
    assert _providers_by_name(status_response.json())["openai"]["configured"] is False
    assert put_response.status_code == 200
    assert put_response.json() == {"provider": "openai", "configured": True, "enabled": True}
    assert delete_response.status_code == 200
    assert delete_response.json() == {"provider": "openai", "configured": False, "enabled": True}
    assert mock_get_secret.await_count == len(status_providers)
    mock_put_secret.assert_awaited_once_with(
        "openai_api_key",
        "sk-test",
        {"workspace_id": "ws-1"},
    )
    mock_delete_secret.assert_awaited_once_with(
        "openai_api_key",
        {"workspace_id": "ws-1"},
    )


@pytest.mark.anyio
async def test_provider_credential_admin_status_treats_blank_secret_as_missing():
    async def get_secret(secret_name, tenant_scope):
        if secret_name == "openai_api_key":
            return "   "
        raise SecretNotFoundError("missing")

    with patch(
        "app.api.handlers_llm_provider_admin.secret_store.get_secret",
        new_callable=AsyncMock,
        side_effect=get_secret,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get(
                "/api/v1/internal/llm/provider-credentials?workspace_id=ws-1",
                headers=ADMIN_HEADERS,
            )

    assert response.status_code == 200
    assert _providers_by_name(response.json())["openai"] == {
        "provider": "openai",
        "configured": False,
        "enabled": True,
    }


@pytest.mark.anyio
async def test_provider_credential_admin_rejects_blank_api_key():
    with patch(
        "app.api.handlers_llm_provider_admin.secret_store.put_secret",
        new_callable=AsyncMock,
    ) as mock_put_secret:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.put(
                "/api/v1/internal/llm/provider-credentials/openai",
                headers=ADMIN_HEADERS,
                json={"workspace_id": "ws-1", "api_key": "   "},
            )

    assert response.status_code == 422
    mock_put_secret.assert_not_awaited()


@pytest.mark.anyio
async def test_provider_credential_admin_trims_api_key_and_workspace_id():
    with patch(
        "app.api.handlers_llm_provider_admin.secret_store.put_secret",
        new_callable=AsyncMock,
    ) as mock_put_secret:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.put(
                "/api/v1/internal/llm/provider-credentials/openai",
                headers=ADMIN_HEADERS,
                json={"workspace_id": " ws-1 ", "api_key": " sk-test "},
            )

    assert response.status_code == 200
    mock_put_secret.assert_awaited_once_with(
        "openai_api_key",
        "sk-test",
        {"workspace_id": "ws-1"},
    )


@pytest.mark.anyio
async def test_provider_credential_admin_rejects_blank_workspace_query():
    with patch(
        "app.api.handlers_llm_provider_admin.secret_store.delete_secret",
        new_callable=AsyncMock,
    ) as mock_delete_secret:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.delete(
                "/api/v1/internal/llm/provider-credentials/openai?workspace_id=%20%20%20",
                headers=ADMIN_HEADERS,
            )

    assert response.status_code == 422
    mock_delete_secret.assert_not_awaited()


@pytest.mark.anyio
async def test_provider_credential_admin_rejects_blank_provider_path():
    with patch(
        "app.api.handlers_llm_provider_admin.secret_store.delete_secret",
        new_callable=AsyncMock,
    ) as mock_delete_secret:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.delete(
                "/api/v1/internal/llm/provider-credentials/%20%20%20?workspace_id=ws-1",
                headers=ADMIN_HEADERS,
            )

    assert response.status_code == 422
    assert response.json()["detail"] == "provider must not be blank"
    mock_delete_secret.assert_not_awaited()
