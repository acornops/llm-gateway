from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.jwt_validator import JwtValidator


class FakeMetric:
    def __init__(self):
        self.statuses: list[str] = []

    def labels(self, *, status: str):
        self.statuses.append(status)
        return self

    def inc(self):
        return None


def _payload() -> dict:
    return {
        "iss": "issuer",
        "aud": "audience",
        "iat": 1,
        "exp": 2,
        "sub": "user-1",
        "run_id": "run-1",
        "workspace_id": "ws-1",
        "target_id": "cluster-1",
        "target_type": "kubernetes",
        "session_id": "session-1",
        "permissions": {
            "allowed_tools": ["*"],
            "allowed_tool_operations": {"get_resource": "read", "restart_workload": "write"},
        },
    }


@pytest.mark.anyio
async def test_jwt_validator_returns_claims_on_success(monkeypatch: pytest.MonkeyPatch):
    metrics = FakeMetric()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token")
    monkeypatch.setattr(
        "app.auth.jwt_validator.jwks_manager.get_signing_key",
        AsyncMock(return_value="key"),
    )
    monkeypatch.setattr("app.auth.jwt_validator.jwt.decode", lambda *args, **kwargs: _payload())
    monkeypatch.setattr("app.auth.jwt_validator.GATEWAY_JWT_VALIDATIONS_TOTAL", metrics)
    monkeypatch.setattr("app.auth.jwt_validator.settings.AUTH_AUDIENCE", "audience")
    monkeypatch.setattr("app.auth.jwt_validator.settings.AUTH_ISSUER", "issuer")

    claims = await JwtValidator().validate(credentials)

    assert claims.sub == "user-1"
    assert claims.permissions.allowed_tools == ["*"]
    assert claims.permissions.allowed_tool_operations == {
        "get_resource": "read",
        "restart_workload": "write",
    }
    assert metrics.statuses == ["success"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        jwt.InvalidTokenError("bad signature"),
        RuntimeError("unexpected jwks failure"),
    ],
)
async def test_jwt_validator_returns_401_for_validation_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
):
    metrics = FakeMetric()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token")
    monkeypatch.setattr(
        "app.auth.jwt_validator.jwks_manager.get_signing_key",
        AsyncMock(side_effect=error),
    )
    monkeypatch.setattr("app.auth.jwt_validator.GATEWAY_JWT_VALIDATIONS_TOTAL", metrics)

    with pytest.raises(HTTPException, match="Invalid token") as exc_info:
        await JwtValidator().validate(credentials)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"
    assert str(error) not in exc_info.value.detail
    assert metrics.statuses == ["failure"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "decode_error",
    [
        jwt.InvalidIssuerError("issuer mismatch"),
        jwt.InvalidAudienceError("audience mismatch"),
    ],
)
async def test_jwt_validator_returns_401_for_issuer_or_audience_drift(
    monkeypatch: pytest.MonkeyPatch,
    decode_error: Exception,
):
    metrics = FakeMetric()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token")
    monkeypatch.setattr(
        "app.auth.jwt_validator.jwks_manager.get_signing_key",
        AsyncMock(return_value="key"),
    )
    monkeypatch.setattr(
        "app.auth.jwt_validator.jwt.decode",
        MagicMock(side_effect=decode_error),
    )
    monkeypatch.setattr("app.auth.jwt_validator.GATEWAY_JWT_VALIDATIONS_TOTAL", metrics)
    monkeypatch.setattr("app.auth.jwt_validator.settings.AUTH_AUDIENCE", "audience")
    monkeypatch.setattr("app.auth.jwt_validator.settings.AUTH_ISSUER", "issuer")

    with pytest.raises(HTTPException, match="Invalid token") as exc_info:
        await JwtValidator().validate(credentials)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"
    assert metrics.statuses == ["failure"]
