import hashlib
import json
import struct
from pathlib import Path

from app.mcp.canonical_json import canonical_json

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "docs/contracts/canonical-json-vectors.json").read_text()
)


def _parse_ecmascript_json(value: str):
    return json.loads(value, parse_int=float)


def _sampled_finite_doubles(seed: str, count: int) -> list[float]:
    mask = (1 << 64) - 1
    state = int(seed, 16)
    values: list[float] = []
    while len(values) < count:
        state ^= state >> 12
        state ^= (state << 25) & mask
        state ^= state >> 27
        bits = (state * 0x2545F4914F6CDD1D) & mask
        value = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
        if value not in (float("inf"), float("-inf")) and value == value:
            values.append(value)
    return values


def test_shared_canonical_json_vectors():
    for vector in FIXTURE["cases"]:
        canonical = canonical_json(_parse_ecmascript_json(vector["inputJson"]))
        assert canonical == vector["canonical"], vector["name"]
        assert hashlib.sha256(canonical.encode()).hexdigest() == vector["sha256"]


def test_randomized_finite_double_parity_digest():
    sample = FIXTURE["finiteDoubleSample"]
    payload = "\n".join(
        canonical_json(value)
        for value in _sampled_finite_doubles(sample["seed"], sample["count"])
    ) + "\n"
    assert hashlib.sha256(payload.encode()).hexdigest() == sample[
        "newlineDelimitedCanonicalSha256"
    ]
