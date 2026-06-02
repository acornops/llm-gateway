import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.config.settings import settings
from app.main import app


@pytest.mark.anyio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_request_id_header_is_returned():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health", headers={"X-Request-Id": "req-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "req-123"


@pytest.mark.anyio
async def test_oversized_request_body_is_rejected_before_routing():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/health",
            content=b"x" * (settings.MAX_REQUEST_BODY_BYTES + 1),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_ready_reports_required_dependency_checks(monkeypatch: pytest.MonkeyPatch):
    async def fake_database_check():
        return {"ok": True, "required": True, "detail": "Database connection succeeded."}

    async def fake_redis_check():
        return {
            "ok": True,
            "required": False,
            "detail": "Redis not configured; Redis-backed features are disabled.",
        }

    async def fake_jwks_check():
        return {
            "ok": True,
            "required": True,
            "detail": "JWKS endpoint returned usable signing keys.",
        }

    async def fake_secret_backend_check():
        return {"ok": True, "required": True, "detail": "Secret backend health check succeeded."}

    monkeypatch.setattr("app.main._check_database_ready", fake_database_check)
    monkeypatch.setattr("app.main._check_redis_ready", fake_redis_check)
    monkeypatch.setattr("app.main._check_jwks_ready", fake_jwks_check)
    monkeypatch.setattr("app.main._check_secret_backend_ready", fake_secret_backend_check)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database": {
                "ok": True,
                "required": True,
                "detail": "Database connection succeeded.",
            },
            "redis": {
                "ok": True,
                "required": False,
                "detail": "Redis not configured; Redis-backed features are disabled.",
            },
            "jwks": {
                "ok": True,
                "required": True,
                "detail": "JWKS endpoint returned usable signing keys.",
            },
            "secret_backend": {
                "ok": True,
                "required": True,
                "detail": "Secret backend health check succeeded.",
            },
        },
    }


@pytest.mark.anyio
async def test_ready_returns_503_when_database_check_fails(monkeypatch: pytest.MonkeyPatch):
    async def fake_database_check():
        return {
            "ok": False,
            "required": True,
            "detail": "Database readiness check failed: connection refused",
        }

    async def fake_redis_check():
        return {"ok": True, "required": True, "detail": "Redis ping succeeded."}

    async def fake_jwks_check():
        return {
            "ok": True,
            "required": True,
            "detail": "JWKS endpoint returned usable signing keys.",
        }

    async def fake_secret_backend_check():
        return {"ok": True, "required": True, "detail": "Secret backend health check succeeded."}

    monkeypatch.setattr("app.main._check_database_ready", fake_database_check)
    monkeypatch.setattr("app.main._check_redis_ready", fake_redis_check)
    monkeypatch.setattr("app.main._check_jwks_ready", fake_jwks_check)
    monkeypatch.setattr("app.main._check_secret_backend_ready", fake_secret_backend_check)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "message": "Readiness checks failed for required dependencies.",
        "failed_checks": ["database"],
        "checks": {
            "database": {
                "ok": False,
                "required": True,
                "detail": "Database readiness check failed: connection refused",
            },
            "redis": {
                "ok": True,
                "required": True,
                "detail": "Redis ping succeeded.",
            },
            "jwks": {
                "ok": True,
                "required": True,
                "detail": "JWKS endpoint returned usable signing keys.",
            },
            "secret_backend": {
                "ok": True,
                "required": True,
                "detail": "Secret backend health check succeeded.",
            },
        },
    }


@pytest.mark.anyio
async def test_ready_returns_503_when_enabled_redis_check_fails(monkeypatch: pytest.MonkeyPatch):
    async def fake_database_check():
        return {"ok": True, "required": True, "detail": "Database connection succeeded."}

    async def fake_redis_check():
        return {
            "ok": False,
            "required": True,
            "detail": "Redis readiness check failed: timed out",
        }

    async def fake_jwks_check():
        return {
            "ok": True,
            "required": True,
            "detail": "JWKS endpoint returned usable signing keys.",
        }

    async def fake_secret_backend_check():
        return {"ok": True, "required": True, "detail": "Secret backend health check succeeded."}

    monkeypatch.setattr("app.main._check_database_ready", fake_database_check)
    monkeypatch.setattr("app.main._check_redis_ready", fake_redis_check)
    monkeypatch.setattr("app.main._check_jwks_ready", fake_jwks_check)
    monkeypatch.setattr("app.main._check_secret_backend_ready", fake_secret_backend_check)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/ready")

    assert response.status_code == 503
    assert response.json()["failed_checks"] == ["redis"]
    assert response.json()["checks"]["redis"]["detail"] == (
        "Redis readiness check failed: timed out"
    )


@pytest.mark.anyio
async def test_ready_returns_503_when_jwks_required_check_fails(monkeypatch: pytest.MonkeyPatch):
    async def fake_database_check():
        return {"ok": True, "required": True, "detail": "Database connection succeeded."}

    async def fake_redis_check():
        return {"ok": True, "required": True, "detail": "Redis ping succeeded."}

    async def fake_jwks_check():
        return {
            "ok": False,
            "required": True,
            "detail": "JWKS readiness check failed.",
        }

    async def fake_secret_backend_check():
        return {"ok": True, "required": True, "detail": "Secret backend health check succeeded."}

    monkeypatch.setattr("app.main._check_database_ready", fake_database_check)
    monkeypatch.setattr("app.main._check_redis_ready", fake_redis_check)
    monkeypatch.setattr("app.main._check_jwks_ready", fake_jwks_check)
    monkeypatch.setattr("app.main._check_secret_backend_ready", fake_secret_backend_check)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/ready")

    assert response.status_code == 503
    assert response.json()["failed_checks"] == ["jwks"]


@pytest.mark.anyio
async def test_secret_backend_readiness_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch):
    class FailingSecretStore:
        async def health_check(self):
            raise RuntimeError("vault token s.secret")

    monkeypatch.setattr("app.main.secret_store", FailingSecretStore())

    from app import main as main_module

    result = await main_module._check_secret_backend_ready()

    assert result == {
        "ok": False,
        "required": True,
        "detail": (
            "Secret_backend readiness check failed. "
            "Check secret backend availability and configuration."
        ),
    }


@pytest.mark.anyio
async def test_database_readiness_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch):
    class FailingConnection:
        async def execute(self, _query):
            raise RuntimeError("postgresql://gateway_user:secret@db.internal/gateway")

    class FakeEngine:
        @asynccontextmanager
        async def connect(self):
            yield FailingConnection()

    monkeypatch.setattr("app.main.tool_registry.engine", FakeEngine())

    from app import main as main_module

    result = await main_module._check_database_ready()

    assert result == {
        "ok": False,
        "required": True,
        "detail": (
            "Database readiness check failed. "
            "Check database availability and DATABASE_URL configuration."
        ),
    }


@pytest.mark.anyio
async def test_database_readiness_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch):
    class SlowConnection:
        async def execute(self, _query):
            await asyncio.sleep(0.01)

    class FakeEngine:
        @asynccontextmanager
        async def connect(self):
            yield SlowConnection()

    monkeypatch.setattr("app.main.tool_registry.engine", FakeEngine())
    monkeypatch.setattr(settings, "READINESS_CHECK_TIMEOUT_MS", 1)

    from app import main as main_module

    result = await main_module._check_database_ready()

    assert result == {
        "ok": False,
        "required": True,
        "detail": (
            "Database readiness check timed out after 1ms. "
            "Check database availability and DATABASE_URL configuration."
        ),
    }


@pytest.mark.anyio
async def test_redis_readiness_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch):
    class FailingRedis:
        async def ping(self):
            raise RuntimeError("redis://:secret@redis.internal:6379/0")

    monkeypatch.setattr(settings, "REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setattr("app.main.rate_limiter", SimpleNamespace(redis=FailingRedis()))

    from app import main as main_module

    result = await main_module._check_redis_ready()

    assert result == {
        "ok": False,
        "required": True,
        "detail": (
            "Redis readiness check failed. "
            "Check Redis availability and REDIS_URL configuration."
        ),
    }
