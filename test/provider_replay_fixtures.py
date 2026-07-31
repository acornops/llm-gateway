"""Helpers for sanitized provider stream replay fixtures."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def load_replay_cases(filename: str) -> list[dict[str, Any]]:
    """Load a provider replay fixture file."""

    payload = json.loads((FIXTURE_ROOT / filename).read_text())
    if not isinstance(payload, list):
        raise ValueError(f"{filename} must contain a list of replay cases")
    return payload


def _to_field(key: str, value: Any) -> Any:
    if key == "args" and isinstance(value, dict):
        return {
            nested_key: to_namespace(nested_value)
            for nested_key, nested_value in value.items()
        }
    return to_namespace(value)


def to_namespace(value: Any) -> Any:
    """Convert fixture JSON into SDK-like objects, including byte markers."""

    if isinstance(value, dict):
        if set(value) == {"__bytes_b64__"}:
            return base64.b64decode(value["__bytes_b64__"], validate=True)
        return SimpleNamespace(
            **{key: _to_field(key, item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [to_namespace(item) for item in value]
    return value
