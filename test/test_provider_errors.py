import httpx
import pytest
from openai import APIError, RateLimitError

from app.llm.adapters.provider_errors import provider_failure_event


class StructuredProviderError(Exception):
    def __init__(
        self,
        *,
        status_code: int | None = None,
        code: int | None = None,
        body: object | None = None,
        details: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.body = body
        self.details = details
        super().__init__("provider-owned message must not drive classification")


def event_dict(error: BaseException, *, retryable: bool = False) -> dict:
    return provider_failure_event(
        error,
        fallback_code="OPENAI_ERROR",
        retryable=retryable,
    ).model_dump(exclude_none=True)


def test_classifies_only_explicit_structured_model_codes_as_unavailable() -> None:
    assert event_dict(
        StructuredProviderError(
            status_code=404,
            body={"error": {"code": "model_not_found", "message": "ignored"}},
        )
    ) == {
        "type": "error",
        "code": "MODEL_UNAVAILABLE",
        "message": "Selected model is unavailable",
        "retryable": False,
    }


def test_does_not_infer_model_unavailability_from_status_or_message() -> None:
    assert event_dict(
        StructuredProviderError(
            status_code=404,
            body={"error": {"type": "not_found_error", "message": "model not found"}},
        )
    ) == {
        "type": "error",
        "code": "OPENAI_ERROR",
        "message": "Provider request failed",
        "retryable": False,
    }


def test_classifies_authentication_and_rate_limit_statuses() -> None:
    assert event_dict(StructuredProviderError(status_code=401))["code"] == "PROVIDER_AUTH_INVALID"
    assert event_dict(StructuredProviderError(status_code=429), retryable=True) == {
        "type": "error",
        "code": "PROVIDER_RATE_LIMITED",
        "message": "Provider rate limit reached",
        "retryable": True,
    }


def test_classifies_retryable_transport_failures_without_exposing_details() -> None:
    request = httpx.Request("POST", "https://provider.example/v1/messages")
    error = httpx.ConnectError("secret upstream detail", request=request)

    assert event_dict(error, retryable=True) == {
        "type": "error",
        "code": "PROVIDER_UNAVAILABLE",
        "message": "Provider temporarily unavailable",
        "retryable": True,
    }


def test_does_not_classify_from_retryability_or_message_text() -> None:
    error = StructuredProviderError(status_code=400)
    error.args = ("temporary timeout while resolving model",)

    assert event_dict(error, retryable=True) == {
        "type": "error",
        "code": "OPENAI_ERROR",
        "message": "Provider request failed",
        "retryable": True,
    }


def test_supports_nested_structured_details_and_numeric_code() -> None:
    error = StructuredProviderError(
        code=404,
        body={},
        details={"error": {"code": "MODEL_NOT_FOUND"}},
    )

    assert event_dict(error) == {
        "type": "error",
        "code": "MODEL_UNAVAILABLE",
        "message": "Selected model is unavailable",
        "retryable": False,
    }


@pytest.mark.parametrize("status_code", [None, 429])
def test_credit_exhaustion_is_actionable_and_not_a_retryable_rate_limit(status_code) -> None:
    body = {
        "code": "credit_balance_exhausted", "type": "insufficient_quota",
        "message": "secret detail",
    }
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    error = (
        APIError("secret detail", request, body=body)
        if status_code is None
        else RateLimitError(
            "secret detail", response=httpx.Response(429, request=request), body=body,
        )
    )
    result = event_dict(error, retryable=True)

    assert result["code"] == "OPENAI_ERROR"
    assert result["retryable"] is False
    assert "no credits remaining" in result["message"]
    assert "workspace administrator" in result["message"]
    assert "AI Settings" in result["message"]
    assert "secret detail" not in result["message"]


@pytest.mark.parametrize("code", [
    "insufficient_quota", "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "usage_limit_exceeded",
])
def test_billing_limits_do_not_claim_exhausted_credits_or_suggest_retrying(code) -> None:
    result = event_dict(
        StructuredProviderError(status_code=429, body={"error": {"code": code}}), retryable=True,
    )

    assert result["code"] == "OPENAI_ERROR"
    assert result["retryable"] is False
    assert "billing or usage limit" in result["message"]
    assert "AI Settings" in result["message"]
    assert "no credits" not in result["message"]


def test_quota_type_is_recognized_but_message_text_is_not() -> None:
    result = event_dict(StructuredProviderError(body={"type": "insufficient_quota"}))
    assert "billing or usage limit" in result["message"]
    unknown = StructuredProviderError(
        body={"code": "unknown", "message": "You have no credits remaining"},
    )
    unknown.args = ("You have no credits remaining",)
    assert event_dict(unknown)["message"] == "Provider request failed"
