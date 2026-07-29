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
        "npx @acme/server",
    ],
)
def test_manual_mcp_endpoint_rejects_unsafe_url_forms(endpoint: str) -> None:
    with pytest.raises(HTTPException):
        validate_remote_mcp_endpoint_contract(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://mcp.internal.example/mcp?tenant=operations",
        "https://gitlab.com/api/v4/mcp",
        "https://github.com/acme/server",
        "https://registry.example/v0.1",
        "https://registry.example/server.json",
        "https://mcp.example/service.git",
    ],
)
def test_manual_mcp_endpoint_allows_opaque_https_paths(endpoint: str) -> None:
    validate_remote_mcp_endpoint_contract(endpoint)
