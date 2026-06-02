import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api.router import api_router
from app.auth.jwks import jwks_manager
from app.config.settings import settings
from app.errors.codes import ErrorCode
from app.errors.envelope import ErrorEnvelope
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.mcp.transports.http_transport import mcp_transport
from app.observability.metrics import (
    GATEWAY_HTTP_REQUEST_DURATION_MS,
    GATEWAY_HTTP_REQUESTS_TOTAL,
    GATEWAY_READINESS_CHECK,
)
from app.resilience.rate_limit import rate_limiter
from app.secrets.store import secret_store

# Setup logging
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(settings.LOG_LEVEL.upper()),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class RequestBodyTooLargeError(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_body_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await self._send_too_large(scope, receive, send)
                    return
            except ValueError:
                await self._send_too_large(scope, receive, send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestBodyTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLargeError:
            await self._send_too_large(scope, receive, send)

    async def _send_too_large(self, scope, receive, send):
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content=ErrorEnvelope(
                error={
                    "code": ErrorCode.VALIDATION_ERROR,
                    "message": "Request body exceeds the configured size limit",
                    "retryable": False,
                }
            ).model_dump(),
        )
        await response(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Starting llm-gateway", version="0.0.1-experimental.1")

    # Setup Tracing
    provider = TracerProvider()
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    await tool_registry.start_cache_invalidation_listener()
    await secret_store.start_cache_invalidation_listener()

    yield

    # Shutdown logic
    await mcp_transport.close()
    await secret_store.close()
    await tool_registry.close()
    await mcp_server_registry.close()
    await jwks_manager.close()
    logger.info("Shutting down llm-gateway")


app = FastAPI(
    title="llm-gateway",
    version="0.0.1-experimental.1",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=settings.MAX_REQUEST_BODY_BYTES,
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    start_time = time.time()

    span_context = trace.get_current_span().get_span_context()
    trace_id = None
    if span_context.is_valid:
        trace_id = f"{span_context.trace_id:032x}"

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
    )

    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "http_request_completed",
            status=status_code,
            duration_ms=duration_ms,
        )
        endpoint = (
            request.scope.get("route").path
            if request.scope.get("route")
            else request.url.path
        )
        GATEWAY_HTTP_REQUESTS_TOTAL.labels(
            endpoint=endpoint,
            method=request.method,
            status=str(status_code),
        ).inc()
        GATEWAY_HTTP_REQUEST_DURATION_MS.labels(endpoint=endpoint).observe(duration_ms)
        structlog.contextvars.clear_contextvars()


async def _run_readiness_check(
    dependency: str,
    operation,
    success_detail: str,
    failure_detail: str,
) -> dict[str, object]:
    timeout_ms = settings.READINESS_CHECK_TIMEOUT_MS
    try:
        async with asyncio.timeout(timeout_ms / 1000.0):
            await operation()
        return {
            "ok": True,
            "required": True,
            "detail": success_detail,
        }
    except TimeoutError:
        logger.warning(
            "readiness_check_timed_out",
            dependency=dependency,
            timeout_ms=timeout_ms,
        )
        return {
            "ok": False,
            "required": True,
            "detail": (
                f"{dependency.capitalize()} readiness check timed out after {timeout_ms}ms. "
                f"{failure_detail}"
            ),
        }
    except Exception as exc:
        logger.warning(
            "readiness_check_failed",
            dependency=dependency,
            error=str(exc),
            exc_info=True,
        )
        return {
            "ok": False,
            "required": True,
            "detail": f"{dependency.capitalize()} readiness check failed. {failure_detail}",
        }


async def _check_database_ready() -> dict[str, object]:
    async def ping_database() -> None:
        async with tool_registry.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return await _run_readiness_check(
        dependency="database",
        operation=ping_database,
        success_detail="Database connection succeeded.",
        failure_detail="Check database availability and DATABASE_URL configuration.",
    )


async def _check_redis_ready() -> dict[str, object]:
    if not settings.REDIS_URL or rate_limiter is None:
        return {
            "ok": True,
            "required": False,
            "detail": "Redis not configured; Redis-backed features are disabled.",
        }

    async def ping_redis() -> None:
        await rate_limiter.redis.ping()

    return await _run_readiness_check(
        dependency="redis",
        operation=ping_redis,
        success_detail="Redis ping succeeded.",
        failure_detail="Check Redis availability and REDIS_URL configuration.",
    )


async def _check_jwks_ready() -> dict[str, object]:
    return await jwks_manager.ensure_ready()


async def _check_secret_backend_ready() -> dict[str, object]:
    async def ping_secret_backend() -> None:
        await secret_store.health_check()

    return await _run_readiness_check(
        dependency="secret_backend",
        operation=ping_secret_backend,
        success_detail="Secret backend health check succeeded.",
        failure_detail="Check secret backend availability and configuration.",
    )


def _record_readiness_metrics(checks: dict[str, dict[str, object]]) -> None:
    for dependency, result in checks.items():
        GATEWAY_READINESS_CHECK.labels(
            dependency=dependency,
            required=str(bool(result["required"])).lower(),
        ).set(1 if result["ok"] else 0)


@app.get("/ready")
async def ready():
    database_check, redis_check, jwks_check, secret_backend_check = await asyncio.gather(
        _check_database_ready(),
        _check_redis_ready(),
        _check_jwks_ready(),
        _check_secret_backend_ready(),
    )
    checks = {
        "database": database_check,
        "redis": redis_check,
        "jwks": jwks_check,
        "secret_backend": secret_backend_check,
    }
    _record_readiness_metrics(checks)
    failed_checks = [
        dependency
        for dependency, result in checks.items()
        if result["required"] and not result["ok"]
    ]
    if failed_checks:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "message": "Readiness checks failed for required dependencies.",
                "failed_checks": failed_checks,
                "checks": checks,
            },
        )
    return {
        "status": "ok",
        "checks": checks,
    }


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Include API router
app.include_router(api_router, prefix="/api/v1")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled_exception", error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorEnvelope(
            error={
                "code": ErrorCode.INTERNAL_ERROR,
                "message": "An internal server error occurred",
                "retryable": False,
                "request_id": request.headers.get("X-Request-Id", "unknown"),
            }
        ).model_dump(),
    )
