from __future__ import annotations

import re
import ssl
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config.settings import settings
from app.resilience.outbound import extract_status_code

_PROVIDER_ROUTES = {
    "openai": (
        "https://api.openai.com/v1/",
        "LLM_PROVIDER_OPENAI_BASE_URL",
    ),
    "anthropic": (
        "https://api.anthropic.com",
        "LLM_PROVIDER_ANTHROPIC_BASE_URL",
    ),
    "gemini": (
        "https://generativelanguage.googleapis.com/",
        "LLM_PROVIDER_GEMINI_BASE_URL",
    ),
}
_MAX_ERROR_LENGTH = 512
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bapi[_-]?key\b\s*[=:]\s*[\"']?)[^\s,;\"']+"),
    re.compile(r"(?i)(\bauthorization\b\s*[=:]\s*[\"']?bearer\s+)[^\s,;\"']+"),
)
_TLS_CERTIFICATE_MARKERS = (
    "certificate_verify_failed",
    "certificate verify failed",
    "hostname mismatch",
    "self signed certificate",
    "unable to get local issuer certificate",
    "certificate has expired",
)
_DNS_MARKERS = (
    "getaddrinfo",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no such host",
)


def sanitize_provider_url(value: str) -> str:
    """Remove credentials and non-routing URL data before logging."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return "<invalid-provider-url>"
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))
    except ValueError:
        return "<invalid-provider-url>"


def provider_base_url(provider: str) -> str:
    """Return the canonical AcornOps route for a supported provider."""
    normalized = provider.strip().lower()
    route = _PROVIDER_ROUTES.get(normalized)
    if route is None:
        raise ValueError(f"Unsupported LLM provider: {provider}")
    default_url, url_setting = route
    configured_url = getattr(settings, url_setting)
    return configured_url.strip() if configured_url and configured_url.strip() else default_url


def provider_route_log_fields(
    provider: str,
) -> dict[str, Any]:
    """Return the sanitized effective route for a supported provider."""
    normalized = provider.strip().lower()
    route = _PROVIDER_ROUTES.get(normalized)
    if route is None:
        fields: dict[str, Any] = {
            "provider": normalized,
            "provider_base_url": "<invalid-provider-url>",
            "custom_base_url": False,
            "base_url_source": "invalid",
        }
        return fields

    _, url_setting = route
    configured_url = getattr(settings, url_setting)
    source = "acornops_setting" if configured_url and configured_url.strip() else "default"
    effective_url = provider_base_url(normalized)
    fields = {
        "provider": normalized,
        "provider_base_url": sanitize_provider_url(effective_url),
        "custom_base_url": source != "default",
        "base_url_source": source,
    }
    return fields


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def _safe_exception_message(exc: BaseException) -> str:
    message = str(exc).replace("\n", " ").replace("\r", " ")
    message = _URL_PATTERN.sub(
        lambda match: sanitize_provider_url(match.group(0)),
        message,
    )
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub(r"\1<redacted>", message)
    if len(message) <= _MAX_ERROR_LENGTH:
        return message
    return f"{message[: _MAX_ERROR_LENGTH - 3]}..."


def classify_provider_error(exc: BaseException) -> str:
    """Map provider failures to bounded operator-facing categories."""
    status_code = extract_status_code(exc)
    if status_code is not None:
        if 400 <= status_code < 500:
            return "http_4xx"
        if 500 <= status_code < 600:
            return "http_5xx"
        return "http_other"

    chain = _exception_chain(exc)
    names = " ".join(item.__class__.__name__.lower() for item in chain)
    messages = " ".join(str(item).lower() for item in chain)
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain) or any(
        marker in messages for marker in _TLS_CERTIFICATE_MARKERS
    ):
        return "tls_certificate_verification"
    if any(isinstance(item, ssl.SSLError) for item in chain) or "sslerror" in names:
        return "tls"
    if "gaierror" in names or any(marker in messages for marker in _DNS_MARKERS):
        return "dns"
    if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in chain):
        return "timeout"
    if any(isinstance(item, (ConnectionError, httpx.RequestError)) for item in chain):
        return "connect"
    return "other"


def provider_failure_log_fields(
    *,
    provider: str,
    model: str,
    run_id: str,
    workspace_id: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Build safe structured fields for a provider request failure."""
    root_cause = _exception_chain(exc)[-1]
    fields = {
        **provider_route_log_fields(provider),
        "model": model,
        "run_id": run_id,
        "workspace_id": workspace_id,
        "error_type": exc.__class__.__name__,
        "error": _safe_exception_message(exc),
        "error_category": classify_provider_error(exc),
        "root_cause_type": root_cause.__class__.__name__,
        "root_cause": _safe_exception_message(root_cause),
        "additional_ca_configured": bool(settings.ADDITIONAL_CA_BUNDLE_FILE),
    }
    status_code = extract_status_code(exc)
    if status_code is not None:
        fields["http_status"] = status_code
    return fields


def log_provider_stream_failure(
    logger: Any,
    *,
    provider: str,
    model: str,
    run_id: str,
    workspace_id: str,
    attempt: int,
    max_attempts: int,
    emitted_event: bool,
    retryable: bool,
    exc: BaseException,
) -> None:
    """Log a provider failure without changing the client-facing error."""
    logger.warning(
        "provider_stream_failed",
        attempt=attempt,
        max_attempts=max_attempts,
        emitted_event=emitted_event,
        retryable=retryable,
        **provider_failure_log_fields(
            provider=provider,
            model=model,
            run_id=run_id,
            workspace_id=workspace_id,
            exc=exc,
        ),
    )
