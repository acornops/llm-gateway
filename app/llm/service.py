import re
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.examples import (
    EXAMPLE_RUN_ID,
    EXAMPLE_SESSION_ID,
    EXAMPLE_TARGET_ID,
    EXAMPLE_WORKSPACE_ID,
)
from app.llm.transcript import (
    ProviderContinuationState,
    TranscriptTurn,
    validate_transcript_sequence,
)
from app.target_types import KUBERNETES_TARGET_TYPE, TARGET_TYPE_EXAMPLES, TargetType


class ToolSpec(BaseModel):
    name: str = Field(min_length=1, examples=["list_pods"])
    model_name: str | None = Field(default=None, examples=["list_pods"])
    description: str | None = Field(default=None, examples=["List pods in the cluster."])
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": True},
        examples=[{"type": "object", "properties": {"namespace": {"type": "string"}}}],
    )

    @field_validator("name", "model_name")
    @classmethod
    def validate_provider_neutral_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", value):
            raise ValueError(
                "function name must match ^[A-Za-z_][A-Za-z0-9_-]{0,62}$"
            )
        return value

    @property
    def provider_name(self) -> str:
        """Return the optional readable name declared to the model provider."""
        return self.model_name or self.name


class NativeToolSpec(BaseModel):
    id: Literal["web_search"]
    config: dict[str, Any] = Field(default_factory=dict)


SUPPORTED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")


def normalize_provider_name(provider: str) -> str:
    return provider.strip().lower()


class ReasoningConfig(BaseModel):
    summary_mode: Literal["off", "auto", "concise", "detailed"] = "off"
    effort: Literal["off", "low", "medium", "high"] = "off"


class RequestScope(BaseModel):
    type: Literal["target", "agent_chat", "workspace"] = "target"


def reasoning_summaries_enabled(req: "NormalizedLLMRequest") -> bool:
    return req.reasoning.summary_mode != "off"


def model_reasoning_enabled(req: "NormalizedLLMRequest") -> bool:
    return reasoning_summaries_enabled(req) or req.reasoning.effort != "off"


class NormalizedLLMRequest(BaseModel):
    run_id: str = Field(examples=[EXAMPLE_RUN_ID])
    workspace_id: str = Field(examples=[EXAMPLE_WORKSPACE_ID])
    scope: RequestScope = Field(default_factory=RequestScope)
    target_id: str | None = Field(default=None, examples=[EXAMPLE_TARGET_ID])
    target_type: TargetType | None = Field(default=None, examples=TARGET_TYPE_EXAMPLES)
    workflow_id: str | None = None
    execution_id: str | None = None
    workflow_session_id: str | None = None
    executor_role: Literal["coordinator", "specialist"] | None = None
    agent_id: str | None = None
    trigger_id: str | None = None
    session_id: str = Field(examples=[EXAMPLE_SESSION_ID])
    provider: Literal["openai", "anthropic", "gemini"] = Field(examples=["gemini"])
    model: str = Field(examples=["gemini-2.0-flash"])
    runtime_instruction: str = Field(min_length=1, max_length=65536)
    transcript: list[TranscriptTurn] = Field(min_length=1)
    tools: list[ToolSpec] = []
    native_tools: list[NativeToolSpec] = []
    temperature: float = 0.7
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)

    @model_validator(mode="after")
    def validate_scope_fields(self):
        if self.scope.type == "target":
            if not self.target_id or not self.target_type:
                raise ValueError("target scope requires target_id and target_type")
            forbidden = (
                self.workflow_id,
                self.execution_id,
                self.workflow_session_id,
                self.executor_role,
                self.agent_id,
                self.trigger_id,
            )
            if any(value is not None for value in forbidden):
                raise ValueError(
                    "target requests forbid Agent and Workflow identity"
                )
            return self

        if self.scope.type == "agent_chat":
            if not self.agent_id:
                raise ValueError("agent chat requests require agent identity")
            forbidden = (
                self.target_id,
                self.target_type,
                self.workflow_id,
                self.execution_id,
                self.workflow_session_id,
                self.executor_role,
                self.trigger_id,
            )
            if any(value is not None for value in forbidden):
                raise ValueError(
                    "agent chat requests forbid target and workflow fields"
                )
            return self

        missing = [
            name
            for name, value in (
                ("workflow_id", self.workflow_id),
                ("execution_id", self.execution_id),
                ("workflow_session_id", self.workflow_session_id),
                ("executor_role", self.executor_role),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"workspace workflow scope missing required fields: {', '.join(missing)}"
            )
        if self.target_id is not None or self.target_type is not None:
            raise ValueError("workspace workflow requests forbid target identity fields")
        if self.executor_role == "coordinator" and self.agent_id:
            raise ValueError("coordinator workflow requests forbid agent identity")
        if self.executor_role == "specialist" and not self.agent_id:
            raise ValueError("specialist workflow requests require agent identity")
        return self

    @model_validator(mode="after")
    def validate_canonical_transcript(self):
        if not self.runtime_instruction.strip():
            raise ValueError("runtime_instruction must not be blank")
        validate_transcript_sequence(self.transcript, self.provider)
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

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "run_id": EXAMPLE_RUN_ID,
                "workspace_id": EXAMPLE_WORKSPACE_ID,
                "target_id": EXAMPLE_TARGET_ID,
                "target_type": KUBERNETES_TARGET_TYPE,
                "session_id": EXAMPLE_SESSION_ID,
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "runtime_instruction": (
                    "You are AcornOps. Use live tools when target evidence is needed."
                ),
                "transcript": [
                    {
                        "type": "user",
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
                "reasoning": {"summary_mode": "off", "effort": "off"},
            },
        },
    )


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
    provider_state: ProviderContinuationState | None = None
    usage: dict[str, int] | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool | None = None


class LLMAdapter(Protocol):
    async def stream(
        self, req: NormalizedLLMRequest, api_key: str
    ) -> AsyncIterator[StreamEvent]: ...
