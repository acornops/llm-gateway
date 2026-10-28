"""Run the business and probe listeners as one bounded process lifecycle."""

import asyncio
import os
import signal
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext

import uvicorn

from app.config.settings import settings
from app.drain import ServiceDrain


class ServiceServers:
    """One event-loop signal owner; essential failures still fail fast during drain."""

    def __init__(
        self,
        configs: list[uvicorn.Config],
        healthy: Callable[[], bool] = lambda: True,
        *,
        drain: ServiceDrain | None = None,
        shutdown_timeout: float = 20,
    ):
        self.servers = [uvicorn.Server(config) for config in configs]
        self.healthy = healthy
        self.drain = drain or ServiceDrain(settings.SERVICE_DRAIN_TIMEOUT_SECONDS)
        self.shutdown_timeout = min(shutdown_timeout, 20)
        self.stopping = False
        self._watchdog: threading.Timer | None = None

    @property
    def serving(self) -> bool:
        """Draining listeners remain live until their accepted work has finished."""
        business = self.servers[0]
        return business.started and not business.should_exit

    @property
    def accepting(self) -> bool:
        return self.serving and not self.drain.draining

    def _arm_watchdog(self, seconds: float) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        # asyncio.run also waits for cancelled tasks. A task suppressing cancellation,
        # or blocking the loop, must not defeat the process shutdown deadline.
        self._watchdog = threading.Timer(max(seconds, 0), lambda: os._exit(1))
        self._watchdog.daemon = True
        self._watchdog.start()

    def stop(self, *_args) -> None:
        """Close admission once; repeated signals never shorten or extend drain."""
        if self.stopping:
            return
        self.stopping = True
        self.drain.start()
        self._arm_watchdog(self.drain.deadline - time.monotonic() + self.shutdown_timeout)

    @staticmethod
    async def _serve(server: uvicorn.Server) -> None:
        # Uvicorn 0.47 capture_signals is disabled only for these supervised instances.
        server.capture_signals = nullcontext
        try:
            await server.serve()
        except SystemExit as exc:
            raise RuntimeError("Service listener failed to start") from exc

    async def run(self) -> None:
        """Keep serving drain rejections/probes until completion or the first deadline."""
        loop = asyncio.get_running_loop()
        signals = (signal.SIGINT, signal.SIGTERM)
        previous = {sig: signal.getsignal(sig) for sig in signals}
        for sig in signals:
            loop.add_signal_handler(sig, self.stop)
        tasks = [asyncio.create_task(self._serve(server)) for server in self.servers]
        try:
            while True:
                if self.serving and not self.healthy():
                    raise RuntimeError("Essential service task stopped unexpectedly")
                if self.stopping and (
                    self.drain.active == 0 or time.monotonic() >= self.drain.deadline
                ):
                    break
                done, _ = await asyncio.wait(
                    tasks, timeout=0.05, return_when=asyncio.FIRST_COMPLETED
                )
                if done:
                    for task in done:
                        task.result()
                    raise RuntimeError("Service listener stopped unexpectedly")
        finally:
            self.stop()
            # Finishing early and fatal listener/worker exits use only cleanup time.
            remaining = min(
                self.shutdown_timeout,
                max(0, self.drain.deadline + self.shutdown_timeout - time.monotonic()),
            )
            self._arm_watchdog(remaining)
            for server in self.servers:
                server.should_exit = True
                if hasattr(server, "config"):
                    server.config.timeout_graceful_shutdown = max(0, remaining - 1)
            try:
                _, pending = await asyncio.wait(tasks, timeout=max(0, remaining - 0.05))
                for task in pending:
                    task.cancel()
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=0.04)
                # Retrieve failures without waiting indefinitely for cancellation.
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.exception()
                if not pending and not self.drain.tasks and self._watchdog:
                    self._watchdog.cancel()
            finally:
                for sig, handler in previous.items():
                    loop.remove_signal_handler(sig)
                    signal.signal(sig, handler)
