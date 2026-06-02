from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI

from app.config.settings import settings
from app.internal_transport import uvicorn_ssl_kwargs
from app.main import health, logger, ready

probe_app = FastAPI(
    title="llm-gateway probe endpoints",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@probe_app.get("/health")
async def probe_health():
    return await health()


@probe_app.get("/ready")
async def probe_ready():
    return await ready()


async def _serve(config: uvicorn.Config) -> None:
    await uvicorn.Server(config).serve()


async def main() -> None:
    logger.info(
        "llm_gateway_launcher_started",
        internal_transport_tls_enabled=settings.INTERNAL_TRANSPORT_TLS_ENABLED,
        health_port=settings.INTERNAL_TRANSPORT_HEALTH_PORT,
    )
    business = uvicorn.Config(
        "app.main:app",
        host=settings.GATEWAY_HTTP_ADDR,
        port=settings.GATEWAY_PORT,
        log_level=settings.LOG_LEVEL,
        **uvicorn_ssl_kwargs(),
    )
    if not settings.INTERNAL_TRANSPORT_TLS_ENABLED:
        await _serve(business)
        return
    if not settings.INTERNAL_TRANSPORT_HEALTH_PORT:
        raise RuntimeError(
            "INTERNAL_TRANSPORT_HEALTH_PORT is required when internal transport TLS is enabled"
        )
    probe = uvicorn.Config(
        probe_app,
        host=settings.GATEWAY_HTTP_ADDR,
        port=settings.INTERNAL_TRANSPORT_HEALTH_PORT,
        log_level=settings.LOG_LEVEL,
    )
    await asyncio.gather(_serve(business), _serve(probe))


if __name__ == "__main__":
    asyncio.run(main())
