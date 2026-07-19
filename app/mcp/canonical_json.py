"""RFC 8785 JSON canonicalization for approval argument binding."""

import json
import math
from decimal import Decimal
from typing import Any


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("canonical JSON does not support non-finite numbers")
    if value == 0:
        return "0"
    absolute = abs(value)
    shortest = repr(value).lower()
    if value.is_integer() and absolute < 1e21:
        return str(int(value))
    if 1e-6 <= absolute < 1e21:
        return format(Decimal(shortest), "f").rstrip("0").rstrip(".")
    mantissa, exponent = shortest.split("e") if "e" in shortest else (shortest, "0")
    mantissa = mantissa.rstrip("0").rstrip(".")
    exponent_value = int(exponent)
    return f"{mantissa}e{'+' if exponent_value >= 0 else ''}{exponent_value}"


def canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")

        def member(key: str) -> str:
            encoded_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            return f"{encoded_key}:{canonical_json(value[key])}"

        return (
            "{"
            + ",".join(
                member(key)
                for key in sorted(value, key=lambda item: item.encode("utf-16-be", "surrogatepass"))
            )
            + "}"
        )
    raise TypeError("canonical JSON supports only JSON values")
