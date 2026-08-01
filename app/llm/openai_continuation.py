"""Replay-safe OpenAI Responses continuation state."""

from collections.abc import Mapping
from typing import Any

OPENAI_REASONING_INPUT_FIELDS = (
    "type",
    "id",
    "summary",
    "encrypted_content",
)


def project_openai_reasoning_input(item: Mapping[str, Any]) -> dict[str, Any]:
    """Project a provider output item onto the accepted reasoning input shape."""

    if item.get("type") != "reasoning" or "content" in item:
        raise ValueError("OpenAI Responses continuation contains an unsupported item")
    return {
        field: item[field]
        for field in OPENAI_REASONING_INPUT_FIELDS
        if field in item and item[field] is not None
    }


def capture_openai_reasoning_input(item: Any) -> dict[str, Any]:
    """Capture only replay-safe fields from an OpenAI SDK reasoning output item."""

    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        serialized = model_dump(exclude={"content"}, exclude_none=True)
    else:
        serialized = {
            "type": "reasoning",
            "id": str(getattr(item, "id", "") or ""),
            "summary": [
                {
                    "type": str(getattr(summary, "type", "") or ""),
                    "text": str(getattr(summary, "text", "") or ""),
                }
                for summary in (getattr(item, "summary", None) or [])
            ],
            "encrypted_content": getattr(item, "encrypted_content", None),
        }
    return project_openai_reasoning_input(serialized)
