from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.observability.metrics import GATEWAY_UPSTREAM_DEPENDENCY_EVENTS_TOTAL

RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_EXCEPTION_FRAGMENTS = (
    "timeout",
    "connection",
    "ratelimit",
    "serviceunavailable",
    "overloaded",
    "temporarilyunavailable",
    "internalserver",
    "throttl",
)


def note_dependency_event(dependency_type: str, event: str) -> None:
    GATEWAY_UPSTREAM_DEPENDENCY_EVENTS_TOTAL.labels(
        dependency_type=dependency_type,
        event=event,
    ).inc()


def extract_status_code(exc: BaseException) -> int | None:
    direct_status = getattr(exc, "status_code", None)
    if isinstance(direct_status, int):
        return direct_status

    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status

    return None


def is_retryable_dependency_error(exc: BaseException) -> bool:
    if isinstance(exc, CircuitOpenError):
        return False

    if isinstance(exc, (httpx.TimeoutException, httpx.RequestError)):
        return True

    status_code = extract_status_code(exc)
    if status_code in RETRYABLE_HTTP_STATUS_CODES:
        return True

    normalized_name = exc.__class__.__name__.replace("_", "").lower()
    if any(fragment in normalized_name for fragment in _RETRYABLE_EXCEPTION_FRAGMENTS):
        return True

    normalized_message = str(exc).replace(" ", "").lower()
    return any(fragment in normalized_message for fragment in _RETRYABLE_EXCEPTION_FRAGMENTS)


def backoff_seconds(base_backoff_ms: int, attempt: int, max_backoff_ms: int = 2000) -> float:
    bounded_attempt = max(attempt - 1, 0)
    delay_ms = min(base_backoff_ms * (2**bounded_attempt), max_backoff_ms)
    return delay_ms / 1000.0


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    open_until_monotonic: float = 0.0


class CircuitOpenError(RuntimeError):
    def __init__(self, dependency_type: str, dependency_name: str, retry_after_ms: int):
        self.dependency_type = dependency_type
        self.dependency_name = dependency_name
        self.retry_after_ms = retry_after_ms
        super().__init__(
            f"{dependency_type} dependency '{dependency_name}' circuit is open; "
            f"retry after {retry_after_ms}ms."
        )


class DependencyCircuitBreaker:
    def __init__(self) -> None:
        self._states: dict[str, _CircuitState] = {}
        self._lock = asyncio.Lock()

    async def before_call(
        self,
        dependency_key: str,
        dependency_type: str,
        dependency_name: str,
    ) -> None:
        async with self._lock:
            state = self._states.get(dependency_key)
            if state is None:
                return

            now = time.monotonic()
            if state.open_until_monotonic == 0.0:
                return

            if state.open_until_monotonic <= now:
                state.consecutive_failures = 0
                state.open_until_monotonic = 0.0
                return

            retry_after_ms = max(1, int((state.open_until_monotonic - now) * 1000))
            note_dependency_event(dependency_type, "circuit_open")
            raise CircuitOpenError(dependency_type, dependency_name, retry_after_ms)

    async def record_success(self, dependency_key: str) -> None:
        async with self._lock:
            state = self._states.get(dependency_key)
            if state is None:
                return
            state.consecutive_failures = 0
            state.open_until_monotonic = 0.0

    async def record_failure(
        self,
        dependency_key: str,
        failure_threshold: int,
        reset_timeout_ms: int,
    ) -> bool:
        async with self._lock:
            state = self._states.setdefault(dependency_key, _CircuitState())
            state.consecutive_failures += 1
            if state.consecutive_failures < failure_threshold:
                return False

            state.open_until_monotonic = time.monotonic() + (reset_timeout_ms / 1000.0)
            state.consecutive_failures = 0
            return True

    async def reset(self) -> None:
        async with self._lock:
            self._states.clear()


dependency_circuit_breaker = DependencyCircuitBreaker()
