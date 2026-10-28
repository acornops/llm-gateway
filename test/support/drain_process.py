"""Real native launcher fixture: no databases or paid upstreams."""

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

from fastapi.responses import StreamingResponse

import app.main as main
import app.serve as launcher
from app.config.settings import settings
from app.execution_capacity import begin_dispatch, execution_authority

root = Path(os.environ["DRAIN_TEST_ROOT"])
mode = os.environ.get("DRAIN_TEST_MODE", "stream")


def mark(name):
    (root / name).touch()


async def gate(name):
    while not (root / name).exists():
        await asyncio.sleep(0.01)


@asynccontextmanager
async def lifespan(app):
    worker = None
    if mode == "worker_failure":
        worker = asyncio.create_task(gate("fail_worker"))
        main.essential_tasks[:] = [worker]
    yield
    if worker is not None:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    mark("lifespan_cleanup")
    if mode == "deadline_cleanup":
        await asyncio.sleep(0.8)
        mark("lifespan_finished")
    if mode == "stuck_lifespan":
        while True:
            with suppress(asyncio.CancelledError):
                await asyncio.Event().wait()


async def ready_check():
    return {"ok": True, "required": True, "detail": "isolated fixture"}


async def authority_post(run, action, body):
    if action == "operations/finish":
        if mode == "deadline_cleanup":
            await asyncio.sleep(0.8)
        mark("authority_finished")
    return {
        "status": "ok",
        "contractVersion": 1,
        "capacityEnabled": settings.WORKSPACE_CAPACITY_ENABLED,
    }


main.app.router.lifespan_context = lifespan
for dependency in ("database", "redis", "jwks", "secret_backend"):
    setattr(main, f"_check_{dependency}_ready", ready_check)
execution_authority._post = authority_post


@main.app.post("/fixture/write")
async def write():
    mark("receipt_claimed")
    return {"ok": True}


@main.app.get("/fixture/stream")
async def stream():
    async def body():
        async with execution_authority.operation(
            SimpleNamespace(run_id="run", workspace_id="workspace"),
            {"x-acornops-execution-owner": "owner", "x-acornops-execution-generation": "1"},
            100000,
        ):
            await begin_dispatch()
            try:
                yield b"first\n"
                mark("headers_sent")
                if mode == "resist_cancel":
                    while True:
                        with suppress(asyncio.CancelledError):
                            await asyncio.Event().wait()
                await gate("release_body")
                yield b"last\n"
            finally:
                mark("finalizing")
                if mode in {"deadline", "deadline_cleanup"}:
                    (root / "cancelled").write_text(str(time.monotonic()))
                else:
                    await gate("release_cleanup")

    return StreamingResponse(body(), media_type="application/x-ndjson")


@main.app.get("/fixture/authorization")
async def authorization():
    mark("authorization_waiting")
    await gate("release_authorization")
    mark("authorization_completed")
    return {"ok": True}


original_servers = launcher.ServiceServers


def fixture_servers(configs, **kwargs):
    return original_servers(
        configs, **kwargs, shutdown_timeout=float(os.environ.get("DRAIN_TEST_CLEANUP", "2"))
    )


launcher.ServiceServers = fixture_servers
asyncio.run(launcher.main())
