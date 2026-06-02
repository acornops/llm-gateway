import pytest
from fastapi import HTTPException

from app.auth.service_token import require_admin_service_token


@pytest.mark.anyio
@pytest.mark.parametrize("authorization", [None, "", "Token abc"])
async def test_require_admin_service_token_rejects_missing_or_invalid_bearer_prefix(
    authorization: str | None,
):
    with pytest.raises(HTTPException, match="Missing or invalid service token") as exc_info:
        await require_admin_service_token(authorization)

    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_require_admin_service_token_rejects_incorrect_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.auth.service_token.settings.ADMIN_API_TOKEN", "expected-token")

    with pytest.raises(HTTPException, match="Invalid service token") as exc_info:
        await require_admin_service_token("Bearer wrong-token")

    assert exc_info.value.status_code == 401
