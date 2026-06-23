from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

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


class ReasoningConfig(BaseModel):
    summary_mode: Literal["off", "auto", "concise", "detailed"] = "off"
    effort: Literal["default", "low", "medium", "high"] = "default"


class RequestScope(BaseModel):
    type: Literal["target", "workspace"] = "target"


def reasoning_summaries_enabled(req: "NormalizedLLMRequest") -> bool:
    return req.reasoning.summary_mode != "off"


class NormalizedLLMRequest(BaseModel):
    run_id: str = Field(examples=[EXAMPLE_RUN_ID])
    workspace_id: str = Field(examples=[EXAMPLE_WORKSPACE_ID])
    scope: RequestScope = Field(default_factory=RequestScope)
    target_id: str | None = Field(default=None, examples=[EXAMPLE_TARGET_ID])
    target_type: TargetType | None = Field(default=None, examples=TARGET_TYPE_EXAMPLES)
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_session_id: str | None = None
    workflow_step_id: str | None = None
    session_id: str = Field(examples=[EXAMPLE_SESSION_ID])
    provider: Literal["openai", "anthropic", "gemini"] = Field(examples=["gemini"])
    model: str = Field(examples=["gemini-2.0-flash"])
    messages: list[Message]
    tools: list[ToolSpec] = []
    temperature: float = 0.7
    max_output_tokens: int | None = None
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.scope.type == "target":
            if not self.target_id or not self.target_type:
                raise ValueError("target scope requires target_id and target_type")
            return self

        missing = [
            name
            for name, value in (
                ("workflow_id", self.workflow_id),
                ("workflow_run_id", self.workflow_run_id),
                ("workflow_session_id", self.workflow_session_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"workspace workflow scope missing required fields: {', '.join(missing)}")
        if (self.target_id and not self.target_type) or (self.target_type and not self.target_id):
            raise ValueError("workflow target binding requires both target_id and target_type")
        return self

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
                "reasoning": {"summary_mode": "off", "effort": "default"},
            }
        }
    }


class StreamEvent(BaseModel):
    type: str  # "delta", "tool_call", "reasoning_summary_*", "final", "error"
    text: str | None = None
    provider: Literal["openai", "anthropic", "gemini"] | None = None
    reason: Literal[
        "disabled",
        "unsupported_model",
        "unsupported_provider",
        "provider_omitted",
    ] | None = None
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
