from typing import Literal

from pydantic import BaseModel

from app.target_types import TargetType


class Permissions(BaseModel):
    allowed_providers: list[str] = []
    allowed_models: list[str] = []
    allowed_tools: list[str] = []
    allowed_tool_operations: dict[str, Literal["read", "write"]] = {}
    max_output_tokens: int | None = None


class TokenClaims(BaseModel):
    iss: str
    aud: str
    iat: int
    exp: int
    sub: str
    run_id: str
    workspace_id: str
    target_id: str
    target_type: TargetType
    session_id: str
    permissions: Permissions
