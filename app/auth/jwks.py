from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import structlog

from app.config.settings import settings
from app.internal_transport import httpx_tls_kwargs
from app.observability.metrics import (
    GATEWAY_JWKS_REFRESH_AGE_SECONDS,
    GATEWAY_JWKS_REFRESH_TOTAL,
)

logger = structlog.get_logger()


class JwksValidationError(RuntimeError):
    pass


def _validate_jwks_shape(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise JwksValidationError("JWKS payload must be a JSON object")
    keys = payload.get("keys")
    if not isinstance(keys, list) or len(keys) == 0:
        raise JwksValidationError("JWKS payload must contain a non-empty keys array")

    usable = [
        key
        for key in keys
        if isinstance(key, dict)
        and isinstance(key.get("kid"), str)
        and key.get("kid").strip()
        and key.get("kty") == "RSA"
        and key.get("use", "sig") == "sig"
    ]
    if not usable:
        raise JwksValidationError("JWKS payload must contain at least one RSA signing key with kid")

    try:
        jwt.PyJWKSet.from_dict(payload)
    except Exception as exc:
        raise JwksValidationError("JWKS payload contains unusable key material") from exc

    return payload


class JwksManager:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(**httpx_tls_kwargs())
        self._lock = asyncio.Lock()
        self._jwks: dict[str, Any] | None = None
        self._last_success_at: datetime | None = None
        self._last_success_monotonic: float | None = None
        self._last_failure: str | None = None

    @property
    def last_success_at(self) -> datetime | None:
        return self._last_success_at

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    def age_seconds(self) -> float | None:
        if self._last_success_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self._last_success_monotonic)

    def _cache_is_fresh(self) -> bool:
        age = self.age_seconds()
        return age is not None and age <= settings.JWKS_CACHE_TTL_SECONDS

    async def close(self) -> None:
        await self._client.aclose()

    async def refresh(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            if not force and self._jwks is not None and self._cache_is_fresh():
                return self._jwks

            try:
                response = await self._client.get(
                    settings.AUTH_JWKS_URL,
                    timeout=settings.READINESS_CHECK_TIMEOUT_MS / 1000.0,
                )
                response.raise_for_status()
                payload = _validate_jwks_shape(response.json())
            except Exception as exc:
                self._last_failure = str(exc)
                GATEWAY_JWKS_REFRESH_TOTAL.labels(status="failure").inc()
                logger.warning("jwks_refresh_failed", error=str(exc))
                raise

            self._jwks = payload
            self._last_success_at = datetime.now(UTC)
            self._last_success_monotonic = time.monotonic()
            self._last_failure = None
            GATEWAY_JWKS_REFRESH_TOTAL.labels(status="success").inc()
            GATEWAY_JWKS_REFRESH_AGE_SECONDS.set(0)
            return payload

    async def ensure_ready(self) -> dict[str, object]:
        required = bool(settings.REQUIRE_JWKS_READINESS)
        try:
            if self._jwks is None or not self._cache_is_fresh():
                await self.refresh(force=True)
        except Exception:
            age = self.age_seconds()
            if (
                not required
                or (
                    age is not None
                    and age <= settings.JWKS_READINESS_MAX_STALENESS_SECONDS
                )
            ):
                return {
                    "ok": True,
                    "required": required,
                    "detail": "JWKS refresh failed; using cached keys within staleness budget.",
                    "last_success_at": self._last_success_at.isoformat()
                    if self._last_success_at
                    else None,
                    "last_failure": self._last_failure,
                }
            return {
                "ok": False,
                "required": required,
                "detail": (
                    "JWKS readiness check failed. "
                    "Check AUTH_JWKS_URL and control-plane JWKS availability."
                ),
                "last_success_at": self._last_success_at.isoformat()
                if self._last_success_at
                else None,
                "last_failure": self._last_failure,
            }

        age = self.age_seconds()
        if age is not None:
            GATEWAY_JWKS_REFRESH_AGE_SECONDS.set(age)
        return {
            "ok": True,
            "required": required,
            "detail": "JWKS endpoint returned usable signing keys.",
            "last_success_at": self._last_success_at.isoformat()
            if self._last_success_at
            else None,
        }

    async def get_signing_key(self, token: str):
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise
        except Exception as exc:
            raise jwt.InvalidTokenError("Invalid JWT header") from exc

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise jwt.InvalidTokenError("JWT header is missing kid")

        jwks = await self.refresh()
        key = self._find_key(jwks, kid)
        if key is not None:
            return key

        jwks = await self.refresh(force=True)
        key = self._find_key(jwks, kid)
        if key is None:
            raise jwt.InvalidTokenError("Unable to find matching JWKS key")
        return key

    @staticmethod
    def _find_key(jwks: dict[str, Any], kid: str):
        jwk_set = jwt.PyJWKSet.from_dict(jwks)
        for jwk in jwk_set.keys:
            if jwk.key_id == kid:
                return jwk.key
        return None


jwks_manager = JwksManager()
