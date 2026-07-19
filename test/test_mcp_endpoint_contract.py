import pytest
from fastapi import HTTPException

from app.api.mcp_admin_validation import validate_remote_mcp_endpoint_contract


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example/mcp",
        "https://user:password@mcp.example/mcp",
        "https://mcp.example/mcp#fragment",
        "https://mcp.example/mcp?token=secret",
        "https://github.com/acme/server",
        "https://registry.example/v0.1",
        "https://registry.example/v0.1/servers",
        "https://registry.example/server.json",
        "npx @acme/server",
    ],
)
def test_manual_mcp_endpoint_rejects_non_endpoint_locations(endpoint: str) -> None:
    with pytest.raises(HTTPException):
        validate_remote_mcp_endpoint_contract(endpoint)


def test_manual_mcp_endpoint_allows_non_secret_query_parameters() -> None:
    validate_remote_mcp_endpoint_contract(
        "https://mcp.internal.example/mcp?tenant=operations"
    )
