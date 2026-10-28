import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.serve as launcher
from app.config.settings import settings


@pytest.fixture(autouse=True)
def isolated_launcher_drain(monkeypatch):
    from app.drain import ServiceDrain

    monkeypatch.setattr(launcher, "service_drain", ServiceDrain(1))


@pytest.mark.asyncio
async def test_probe_rechecks_listener_after_awaiting_readiness(monkeypatch):
    listener = SimpleNamespace(serving=True)

    async def stop_during_readiness():
        listener.serving = False
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(launcher, "servers", listener)
    monkeypatch.setattr(launcher, "ready", stop_during_readiness)
    assert (await launcher.probe_ready()).status_code == 503


@pytest.mark.asyncio
async def test_probe_requires_a_started_business_listener(monkeypatch):
    async def ready():
        return {"status": "ok"}

    monkeypatch.setattr(launcher, "ready", ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=launcher.probe_app), base_url="http://probe"
    ) as client:
        assert (await client.get("/ready")).status_code == 503
        assert (await client.get("/health")).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_port", [8080, 18080])
async def test_launcher_ends_both_listeners_after_early_return(monkeypatch, failed_port):
    stopped = asyncio.Event()

    class Server:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            self.started = False

        async def serve(self):
            if self.config.port == failed_port:
                self.should_exit = True
                return
            self.started = True
            try:
                while not self.should_exit:
                    await asyncio.sleep(0)
            finally:
                stopped.set()

    monkeypatch.setattr(launcher.uvicorn, "Server", Server)
    monkeypatch.setattr(launcher, "uvicorn_ssl_kwargs", lambda: {})
    monkeypatch.setattr(settings, "GATEWAY_PORT", 8080)
    monkeypatch.setattr(settings, "INTERNAL_TRANSPORT_TLS_ENABLED", True)
    monkeypatch.setattr(settings, "INTERNAL_TRANSPORT_HEALTH_PORT", 18080)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(launcher.main(), 1)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_signal_starts_deadline_even_when_server_never_returns(monkeypatch):
    from app.service_server import ServiceServers

    started, cancelled = asyncio.Event(), asyncio.Event()

    class Server:
        started = True
        should_exit = False

        async def serve(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    real_wait = asyncio.wait

    async def fast_deadline(tasks, *, timeout, return_when=asyncio.ALL_COMPLETED):
        return await real_wait(
            tasks, timeout=0.01 if timeout == 30 else timeout, return_when=return_when
        )

    monkeypatch.setattr(asyncio, "wait", fast_deadline)
    supervisor = ServiceServers([], shutdown_timeout=0.1)
    supervisor.servers = [Server()]
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(started.wait(), 1)
    supervisor.stop()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_drain_keeps_listeners_until_admitted_cleanup_and_ignores_repeat_signal(monkeypatch):
    from app.drain import ServiceDrain
    from app.service_server import ServiceServers

    class Server:
        started = True
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.001)

    drain = ServiceDrain(1)
    drain.active = 1
    supervisor = ServiceServers([], drain=drain, shutdown_timeout=0.2)
    supervisor.servers = [Server()]
    running = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0)
    supervisor.stop()
    deadline = drain.deadline
    supervisor.stop()
    await asyncio.sleep(0.02)
    assert drain.deadline == deadline
    assert supervisor.serving
    assert not supervisor.accepting
    assert not supervisor.servers[0].should_exit
    drain.active = 0
    await asyncio.wait_for(running, 1)


@pytest.mark.asyncio
async def test_worker_failure_still_fails_fast_during_drain():
    from app.drain import ServiceDrain
    from app.service_server import ServiceServers

    class Server:
        started = True
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.001)

    healthy = True
    drain = ServiceDrain(1)
    drain.active = 1
    supervisor = ServiceServers([], healthy=lambda: healthy, drain=drain, shutdown_timeout=0.2)
    supervisor.servers = [Server()]
    running = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0)
    supervisor.stop()
    healthy = False
    with pytest.raises(RuntimeError, match="Essential"):
        await asyncio.wait_for(running, 1)
    drain.active = 0
