"""Conservative client-facing classification for provider request failures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.llm.service import StreamEvent
from app.resilience.outbound import extract_status_code

_MODEL_UNAVAILABLE_CODES = frozenset({"model_not_found"})
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 500, 502, 503, 504})


def _structured_error(exc: BaseException) -> Mapping[str, Any]:
    """Return provider-owned structured error fields without parsing messages."""
    for attribute in ("body", "details"):
        value = getattr(exc, attribute, None)
        if not isinstance(value, Mapping):
            continue
        nested = value.get("error")
        if isinstance(nested, Mapping) and nested:
            return nested
        if any(field in value for field in ("code", "type", "param")):
            return value
    return {}


def _provider_status_code(exc: BaseException) -> int | None:
    status_code = extract_status_code(exc)
    if status_code is not None:
        return status_code
    direct_code = getattr(exc, "code", None)
    return direct_code if isinstance(direct_code, int) else None


def _is_model_unavailable(exc: BaseException) -> bool:
    error = _structured_error(exc)
    code = error.get("code")
    return isinstance(code, str) and code.strip().lower() in _MODEL_UNAVAILABLE_CODES


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_transient_provider_failure(exc: BaseException, status_code: int | None) -> bool:
    if status_code in _TRANSIENT_STATUS_CODES:
        return True
    transient_types = (
        TimeoutError,
        ConnectionError,
        httpx.TimeoutException,
        httpx.RequestError,
    )
    return any(
        isinstance(item, transient_types)
        for item in _exception_chain(exc)
    )


def provider_failure_event(
    exc: BaseException,
    *,
    fallback_code: str,
    retryable: bool,
) -> StreamEvent:
    """Build a bounded error event only when provider metadata is decisive."""
    status_code = _provider_status_code(exc)
    if _is_model_unavailable(exc):
        return StreamEvent(
            type="error",
            code="MODEL_UNAVAILABLE",
            message="Selected model is unavailable",
            retryable=False,
        )
    if status_code == 401:
        return StreamEvent(
            type="error",
            code="PROVIDER_AUTH_INVALID",
            message="Provider authentication failed",
            retryable=False,
        )
    if status_code == 429:
        return StreamEvent(
            type="error",
            code="PROVIDER_RATE_LIMITED",
            message="Provider rate limit reached",
            retryable=True,
        )
    if _is_transient_provider_failure(exc, status_code):
        return StreamEvent(
            type="error",
            code="PROVIDER_UNAVAILABLE",
            message="Provider temporarily unavailable",
            retryable=True,
        )
    return StreamEvent(
        type="error",
        code=fallback_code,
        message="Provider request failed",
        retryable=retryable,
    )
