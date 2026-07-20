import json
from pathlib import Path

import pytest

from app.mcp.header_policy import validate_public_headers

VECTORS = json.loads(
    (Path(__file__).parents[1] / "docs/contracts/mcp-public-header-vectors.json").read_text()
)


@pytest.mark.parametrize("vector", VECTORS["cases"], ids=lambda item: item["name"])
def test_public_header_conformance(vector: dict) -> None:
    headers = dict(vector["headers"])
    if vector["valid"]:
        assert validate_public_headers(headers) == headers
    else:
        with pytest.raises(ValueError):
            validate_public_headers(headers)
