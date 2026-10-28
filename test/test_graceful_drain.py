"""Drain admission covers bodies and cleanup, not just response headers."""

import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_drain_tracks_body_and_finalizer_and_rejects_new_work():
    from app.drain import DrainMiddleware, ServiceDrain

    drain = ServiceDrain(1)
    headers_sent, release_body, finalizing, release_cleanup = [asyncio.Event() for _ in range(4)]
    calls = []

    async def handler(scope, receive, send):
        calls.append(scope["path"])
        try:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            headers_sent.set()
            await release_body.wait()
            await send({"type": "http.response.body", "body": b"complete"})
        finally:
            finalizing.set()
            await release_cleanup.wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=DrainMiddleware(handler, drain)),
        base_url="http://gateway",
    ) as client:
        accepted = asyncio.create_task(client.post("/tool"))
        await headers_sent.wait()
        drain.start()
        deadline = drain.deadline
        drain.start()
        assert drain.deadline == deadline
        rejected = await client.post("/tool")
        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "1"
        assert rejected.headers["connection"] == "close"
        assert rejected.json() == {
            "error": {
                "code": "SERVICE_DRAINING",
                "message": "Service is draining",
                "retryable": True,
                "outcome": "not_started",
            }
        }
        assert drain.active == 1
        release_body.set()
        await finalizing.wait()
        assert drain.active == 1
        release_cleanup.set()
        assert (await accepted).content == b"complete"
        assert drain.active == 0
        assert calls == ["/tool"]


@pytest.mark.asyncio
async def test_gateway_rejects_before_receipt_or_auth(monkeypatch):
    import app.main as main
    from app.drain import ServiceDrain

    drain = ServiceDrain(1)
    monkeypatch.setattr(main, "service_drain", drain)
    # Rebuild the middleware stack with this test's coordinator.
    from app.drain import DrainMiddleware

    drain.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=DrainMiddleware(main.app, drain)),
        base_url="http://gateway",
    ) as client:
        response = await client.post("/api/v1/mcp/tool-call", json={"approval_receipt": "unused"})
        assert response.status_code == 503
        assert response.json()["error"]["outcome"] == "not_started"
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
async def test_failed_worker_is_not_healthy_during_drain(monkeypatch):
    import app.main as main

    task = asyncio.create_task(asyncio.sleep(0))
    await task
    monkeypatch.setattr(main, "essential_tasks", [task])
    assert (await main.health()).status_code == 503
