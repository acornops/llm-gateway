import base64
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.jwks import JwksManager
from app.config.settings import settings


def _to_base64url(value: int) -> str:
    return (
        base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))
        .decode("utf-8")
        .rstrip("=")
    )


def _keypair_and_jwk(kid: str):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "kid": kid,
        "n": _to_base64url(numbers.n),
        "e": _to_base64url(numbers.e),
        "alg": "RS256",
    }
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, jwk


@pytest.mark.anyio
@respx.mock
async def test_jwks_readiness_success(monkeypatch: pytest.MonkeyPatch):
    _private_pem, jwk = _keypair_and_jwk("kid-1")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "https://auth.example.test/jwks.json")
    monkeypatch.setattr(settings, "REQUIRE_JWKS_READINESS", True)
    respx.get("https://auth.example.test/jwks.json").mock(
        return_value=httpx.Response(200, json={"keys": [jwk]})
    )
    manager = JwksManager()

    try:
        result = await manager.ensure_ready()
    finally:
        await manager.close()

    assert result["ok"] is True
    assert result["required"] is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "jwks",
    [
        {},
        {"keys": []},
        {"keys": [{"kty": "RSA", "use": "sig"}]},
        {"keys": [{"kty": "EC", "use": "sig", "kid": "kid-1"}]},
    ],
)
@respx.mock
async def test_jwks_readiness_rejects_invalid_shapes(
    monkeypatch: pytest.MonkeyPatch,
    jwks: dict,
):
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "https://auth.example.test/jwks.json")
    monkeypatch.setattr(settings, "REQUIRE_JWKS_READINESS", True)
    respx.get("https://auth.example.test/jwks.json").mock(
        return_value=httpx.Response(200, json=jwks)
    )
    manager = JwksManager()

    try:
        result = await manager.ensure_ready()
    finally:
        await manager.close()

    assert result["ok"] is False
    assert result["required"] is True
    assert "JWKS readiness check failed" in result["detail"]


@pytest.mark.anyio
@respx.mock
async def test_jwks_readiness_uses_cached_keys_within_staleness_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    _private_pem, jwk = _keypair_and_jwk("kid-1")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "https://auth.example.test/jwks.json")
    monkeypatch.setattr(settings, "REQUIRE_JWKS_READINESS", True)
    monkeypatch.setattr(settings, "JWKS_CACHE_TTL_SECONDS", 1)
    monkeypatch.setattr(settings, "JWKS_READINESS_MAX_STALENESS_SECONDS", 900)
    respx.get("https://auth.example.test/jwks.json").mock(side_effect=httpx.ConnectTimeout("boom"))
    manager = JwksManager()
    manager._jwks = {"keys": [jwk]}
    manager._last_success_at = None
    manager._last_success_monotonic = time.monotonic() - 30

    try:
        result = await manager.ensure_ready()
    finally:
        await manager.close()

    assert result["ok"] is True
    assert "using cached keys" in result["detail"]


@pytest.mark.anyio
@respx.mock
async def test_jwt_validation_refreshes_after_key_rotation(monkeypatch: pytest.MonkeyPatch):
    old_private_pem, old_jwk = _keypair_and_jwk("old-kid")
    new_private_pem, new_jwk = _keypair_and_jwk("new-kid")
    monkeypatch.setattr(settings, "AUTH_JWKS_URL", "https://auth.example.test/jwks.json")

    route = respx.get("https://auth.example.test/jwks.json")
    route.side_effect = [
        httpx.Response(200, json={"keys": [old_jwk]}),
        httpx.Response(200, json={"keys": [old_jwk, new_jwk]}),
    ]
    manager = JwksManager()
    claims = {
        "iss": settings.AUTH_ISSUER,
        "aud": settings.AUTH_AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "sub": "test-user",
    }
    old_token = jwt.encode(claims, old_private_pem, algorithm="RS256", headers={"kid": "old-kid"})
    new_token = jwt.encode(claims, new_private_pem, algorithm="RS256", headers={"kid": "new-kid"})

    try:
        old_key = await manager.get_signing_key(old_token)
        jwt.decode(
            old_token,
            old_key,
            algorithms=["RS256"],
            audience=settings.AUTH_AUDIENCE,
            issuer=settings.AUTH_ISSUER,
        )
        new_key = await manager.get_signing_key(new_token)
        decoded = jwt.decode(
            new_token,
            new_key,
            algorithms=["RS256"],
            audience=settings.AUTH_AUDIENCE,
            issuer=settings.AUTH_ISSUER,
        )
    finally:
        await manager.close()

    assert decoded["sub"] == "test-user"
