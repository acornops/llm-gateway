import ssl
from unittest.mock import Mock

import httpx
import openai
import pytest

from app.llm import provider_diagnostics


def test_provider_url_logging_removes_credentials_query_and_fragment() -> None:
    sanitized = provider_diagnostics.sanitize_provider_url(
        "https://operator:top-secret@provider.abc.org:8443/openai/v1"
        "?api_key=hidden#debug"
    )

    assert sanitized == "https://provider.abc.org:8443/openai/v1"
    assert "top-secret" not in sanitized
    assert "hidden" not in sanitized


@pytest.mark.parametrize(
    "value",
    ["provider.abc.org/v1", "file:///etc/provider", "https://provider.abc.org:invalid"],
)
def test_invalid_provider_urls_are_not_logged(value: str) -> None:
    assert provider_diagnostics.sanitize_provider_url(value) == "<invalid-provider-url>"


def test_route_fields_report_sanitized_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_diagnostics.settings,
        "LLM_PROVIDER_OPENAI_BASE_URL",
        "https://user:secret@openai.abc.org/v1?token=hidden",
    )
    fields = provider_diagnostics.provider_route_log_fields("openai")

    assert fields == {
        "provider": "openai",
        "provider_base_url": "https://openai.abc.org/v1",
        "custom_base_url": True,
        "base_url_source": "acornops_setting",
        "api_surface": "responses",
    }


def test_route_ignores_sdk_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_diagnostics.settings, "LLM_PROVIDER_OPENAI_BASE_URL", None)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.abc.org/v1")

    fields = provider_diagnostics.provider_route_log_fields("openai")

    assert provider_diagnostics.provider_base_url("openai") == "https://api.openai.com/v1/"
    assert fields["provider_base_url"] == "https://api.openai.com/v1/"
    assert fields["base_url_source"] == "default"


def test_openai_route_fields_report_configured_api_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_diagnostics.settings,
        "LLM_PROVIDER_OPENAI_API_SURFACE",
        "chat_completions",
    )

    fields = provider_diagnostics.provider_route_log_fields("openai")

    assert fields["api_surface"] == "chat_completions"


def test_wrapped_sdk_certificate_error_exposes_safe_root_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_diagnostics.settings,
        "LLM_PROVIDER_OPENAI_BASE_URL",
        "https://user:secret@provider.abc.org/openai/v1?token=hidden",
    )
    request = httpx.Request("POST", "https://provider.abc.org/openai/v1/responses")
    certificate_error = ssl.SSLCertVerificationError(
        1,
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate",
    )
    connect_error = httpx.ConnectError("TLS connection failed", request=request)
    connect_error.__cause__ = certificate_error
    provider_error = openai.APIConnectionError(request=request)
    provider_error.__cause__ = connect_error

    fields = provider_diagnostics.provider_failure_log_fields(
        provider="openai",
        model="custom-model",
        run_id="run-1",
        workspace_id="workspace-1",
        exc=provider_error,
    )

    assert fields["provider_base_url"] == "https://provider.abc.org/openai/v1"
    assert fields["error_type"] == "APIConnectionError"
    assert fields["error"] == "Connection error."
    assert fields["error_category"] == "tls_certificate_verification"
    assert fields["root_cause_type"] == "SSLCertVerificationError"
    assert "unable to get local issuer certificate" in fields["root_cause"]
    assert fields["additional_ca_configured"] is False
    assert "secret" not in str(fields)
    assert "hidden" not in str(fields)


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (httpx.ConnectTimeout("provider timed out"), "timeout"),
        (httpx.ConnectError("Name or service not known"), "dns"),
        (httpx.ConnectError("connection refused"), "connect"),
    ],
)
def test_connection_failures_use_bounded_categories(
    exc: Exception,
    category: str,
) -> None:
    assert provider_diagnostics.classify_provider_error(exc) == category


def test_provider_http_failure_includes_status() -> None:
    request = httpx.Request("POST", "https://provider.abc.org/v1")
    response = httpx.Response(503, request=request)
    error = httpx.HTTPStatusError("service unavailable", request=request, response=response)

    fields = provider_diagnostics.provider_failure_log_fields(
        provider="openai",
        model="model",
        run_id="run",
        workspace_id="workspace",
        exc=error,
    )

    assert fields["error_category"] == "http_5xx"
    assert fields["http_status"] == 503


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("api_key=sk-provider-secret", "sk-provider-secret"),
        ("Authorization: Bearer bearer-secret", "bearer-secret"),
        (
            "failed https://user:url-secret@provider.abc.org/v1?token=query-secret",
            "query-secret",
        ),
    ],
)
def test_provider_error_messages_redact_credentials(message: str, secret: str) -> None:
    fields = provider_diagnostics.provider_failure_log_fields(
        provider="openai",
        model="model",
        run_id="run",
        workspace_id="workspace",
        exc=RuntimeError(message),
    )

    assert secret not in str(fields)


def test_provider_failure_log_includes_request_and_retry_context() -> None:
    logger = Mock()

    provider_diagnostics.log_provider_stream_failure(
        logger,
        provider="openai",
        model="model",
        run_id="run",
        workspace_id="workspace",
        attempt=2,
        max_attempts=3,
        emitted_event=False,
        retryable=True,
        exc=httpx.ConnectError("connection refused"),
    )

    fields = logger.warning.call_args.kwargs
    assert logger.warning.call_args.args == ("provider_stream_failed",)
    assert fields["provider"] == "openai"
    assert fields["model"] == "model"
    assert fields["run_id"] == "run"
    assert fields["workspace_id"] == "workspace"
    assert fields["attempt"] == 2
    assert fields["error_category"] == "connect"
