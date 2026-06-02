from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from app.examples import (
    EXAMPLE_RUN_ID,
    EXAMPLE_SESSION_ID,
    EXAMPLE_TARGET_ID,
    EXAMPLE_WORKSPACE_ID,
)
from app.target_types import KUBERNETES_TARGET_TYPE, TARGET_TYPE_EXAMPLES, TargetType


class Message(BaseModel):
    role: str = Field(examples=["user"])
    content: str = Field(
        examples=["Investigate CrashLoopBackOff for payments-api in prod namespace."]
    )


class ToolSpec(BaseModel):
    name: str = Field(min_length=1, examples=["list_pods"])
    description: str | None = Field(default=None, examples=["List pods in the cluster."])
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": True},
        examples=[{"type": "object", "properties": {"namespace": {"type": "string"}}}],
    )


SUPPORTED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")


def normalize_provider_name(provider: str) -> str:
    return provider.strip().lower()


class NormalizedLLMRequest(BaseModel):
    run_id: str = Field(examples=[EXAMPLE_RUN_ID])
    workspace_id: str = Field(examples=[EXAMPLE_WORKSPACE_ID])
    target_id: str = Field(examples=[EXAMPLE_TARGET_ID])
    target_type: TargetType = Field(examples=TARGET_TYPE_EXAMPLES)
    session_id: str = Field(examples=[EXAMPLE_SESSION_ID])
    provider: Literal["openai", "anthropic", "gemini"] = Field(examples=["gemini"])
    model: str = Field(examples=["gemini-2.0-flash"])
    messages: list[Message]
    tools: list[ToolSpec] = []
    temperature: float = 0.7
    max_output_tokens: int | None = None

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("provider must be a string")
        normalized = normalize_provider_name(value)
        if normalized not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(
                f"Unsupported provider '{value}'."
                f" Supported providers: {', '.join(SUPPORTED_LLM_PROVIDERS)}."
            )
        return normalized

    model_config = {
        "json_schema_extra": {
            "example": {
                "run_id": EXAMPLE_RUN_ID,
                "workspace_id": EXAMPLE_WORKSPACE_ID,
                "target_id": EXAMPLE_TARGET_ID,
                "target_type": KUBERNETES_TARGET_TYPE,
                "session_id": EXAMPLE_SESSION_ID,
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are AcornOps, a Kubernetes troubleshooting assistant.",
                    },
                    {
                        "role": "user",
                        "content": "Check why payments-api pods are restarting every few minutes.",
                    },
                ],
                "tools": [
                    {
                        "name": "list_pods",
                        "description": "List pods in the cluster.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"namespace": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    }
                ],
                "temperature": 0.2,
                "max_output_tokens": 4000,
            }
        }
    }


class StreamEvent(BaseModel):
    type: str  # "delta", "tool_call", "final", "error"
    text: str | None = None
    call_id: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool | None = None


class LLMAdapter(Protocol):
    async def stream(
        self, req: NormalizedLLMRequest, api_key: str
    ) -> AsyncIterator[StreamEvent]: ...
