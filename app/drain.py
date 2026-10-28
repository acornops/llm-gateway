"""Event-loop-owned admission spanning the complete ASGI request lifetime."""

import asyncio
import time

from starlette.responses import JSONResponse

from app.config.settings import settings
from app.errors.codes import ErrorCode
from app.errors.envelope import ErrorEnvelope


class ServiceDrain:
    """Admission and signal callbacks must run on the same event loop."""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        self.deadline: float | None = None
        self.active = 0
        self.tasks: set[asyncio.Task] = set()
        self.request_tasks: set[asyncio.Task] = set()

    @property
    def draining(self) -> bool:
        return self.deadline is not None

    def start(self) -> None:
        if self.deadline is None:
            self.deadline = time.monotonic() + self.timeout_seconds


class DrainMiddleware:
    """Count accepted work until body delivery and all application finalizers end."""

    def __init__(self, app, drain: ServiceDrain):
        self.app = app
        self.drain = drain

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            task = asyncio.current_task()
            self.drain.tasks.add(task)
            try:
                await self.app(scope, receive, send)
            finally:
                self.drain.tasks.discard(task)
            return
        if scope["type"] != "http" or scope["path"] in {
            "/health",
            "/ready",
            "/metrics",
            "/api/v1/health",
        }:
            await self.app(scope, receive, send)
            return
        # No await between admission and registration: loop signal callbacks cannot
        # interrupt this boundary, including while downstream authorization awaits.
        if self.drain.draining:
            response = JSONResponse(
                status_code=503,
                content=ErrorEnvelope(
                    error={
                        "code": ErrorCode.SERVICE_DRAINING,
                        "message": "Service is draining",
                        "retryable": True,
                        "outcome": "not_started",
                    }
                ).model_dump(exclude_none=True),
                headers={
                    "Retry-After": "1",
                    **(
                        {"Connection": "close"} if scope.get("http_version", "1.1") == "1.1" else {}
                    ),
                },
            )
            await response(scope, receive, send)
            return
        task = asyncio.current_task()
        self.drain.tasks.add(task)
        self.drain.request_tasks.add(task)
        self.drain.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.drain.active -= 1
            self.drain.request_tasks.discard(task)
            self.drain.tasks.discard(task)


service_drain = ServiceDrain(settings.SERVICE_DRAIN_TIMEOUT_SECONDS)


async def wait_retry(delay: float, deadline: float) -> bool:
    """Bound retry pauses by both their own budget and process drain deadline."""
    if service_drain.deadline is not None:
        deadline = min(deadline, service_drain.deadline)
    remaining = deadline - time.monotonic()
    if remaining <= delay:
        return False
    await asyncio.sleep(delay)
    return True


def is_trusted_drain_response(response) -> bool:
    """Only a direct internal response with the exact drain classification is safe."""
    if response.status_code != 503 or response.history:
        return False
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return False
    return isinstance(error, dict) and (
        error.get("code") == "SERVICE_DRAINING"
        and error.get("message") == "Service is draining"
        and error.get("retryable") is True
        and error.get("outcome") == "not_started"
    )
