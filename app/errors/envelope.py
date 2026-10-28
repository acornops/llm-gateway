from typing import Literal

from pydantic import BaseModel

from app.errors.codes import ErrorCode


class ErrorDetails(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: str | None = None
    outcome: Literal["not_started", "unknown"] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetails
