from pydantic import BaseModel

from app.errors.codes import ErrorCode


class ErrorDetails(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetails
