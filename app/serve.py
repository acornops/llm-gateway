from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config.settings import settings
from app.internal_transport import uvicorn_ssl_kwargs
from app.main import health, logger, ready
from app.service_server import ServiceServers

probe_app = FastAPI(
    title="llm-gateway probe endpoints",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
servers: ServiceServers | None = None


def unavailable():
    return JSONResponse(
        status_code=503, content={"status": "not_ready", "detail": "API listener unavailable"}
    )


@probe_app.get("/health")
async def probe_health():
    return await health() if servers and servers.serving else unavailable()


@probe_app.get("/ready")
async def probe_ready():
    return await ready() if servers and servers.serving else unavailable()


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
    configs = [business]
    if settings.INTERNAL_TRANSPORT_TLS_ENABLED:
        if not settings.INTERNAL_TRANSPORT_HEALTH_PORT:
            raise RuntimeError(
                "INTERNAL_TRANSPORT_HEALTH_PORT is required when internal transport TLS is enabled"
            )
        configs.append(
            uvicorn.Config(
                probe_app,
                host=settings.GATEWAY_HTTP_ADDR,
                port=settings.INTERNAL_TRANSPORT_HEALTH_PORT,
                log_level=settings.LOG_LEVEL,
            )
        )
    global servers
    servers = ServiceServers(configs)
    try:
        await servers.run()
    finally:
        servers = None


if __name__ == "__main__":
    asyncio.run(main())
