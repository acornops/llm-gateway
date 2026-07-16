import httpx

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
