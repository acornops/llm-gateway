"""RFC 8785 JSON canonicalization for approval argument binding."""

from typing import Any

import rfc8785

_MAX_SAFE_INTEGER = 2**53 - 1


def _ecmascript_numbers(value: Any) -> Any:
    """Project JSON integers onto the RFC 8785 IEEE-754 input domain."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= _MAX_SAFE_INTEGER else float(value)
    if isinstance(value, list):
        return [_ecmascript_numbers(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _ecmascript_numbers(item) for key, item in value.items()}
    raise TypeError("canonical JSON supports only JSON values")


def canonical_json(value: Any) -> str:
    """Return maintained JCS output using the same number domain as JavaScript."""
    return rfc8785.dumps(_ecmascript_numbers(value)).decode("utf-8")
