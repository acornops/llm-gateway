"""A rejected discovery cannot retain a previously enabled permissive definition."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import mcp_admin_helpers as helpers


@pytest.mark.parametrize("schema", [None, [], "bad", {"enum": list(range(101))}])
def test_present_invalid_discovery_schema_is_not_treated_as_missing(schema):
    with pytest.raises(ValueError, match="TOOL_SCHEMA_REJECTED"):
        helpers._normalize_discovered_tools(
            {"tools": [{"name": "bad-tool", "inputSchema": schema}]}
        )


@pytest.mark.asyncio
async def test_rejected_discovery_disables_exact_stale_tools(monkeypatch):
    payload = {
        "tools": [
            {"name": "bad-a", "inputSchema": {"enum": list(range(101))}},
            {"name": "bad-b", "inputSchema": {}, "outputSchema": []},
            {"name": "good", "inputSchema": {"enum": ["x" * 501]}},
        ]
    }
    monkeypatch.setattr(helpers.mcp_transport, "list_tools", AsyncMock(return_value=payload))
    lookup = AsyncMock(return_value=SimpleNamespace(enabled=True))
    monkeypatch.setattr(helpers.tool_registry, "get_tool", lookup)
    disable = AsyncMock()
    monkeypatch.setattr(helpers, "_apply_tools_for_server", disable)
    server = SimpleNamespace(
        id="server-1",
        server_url="https://example.test/mcp",
        target_type="kubernetes",
        scope_type="target",
        server_name="test",
    )
    tools, error, code = await helpers._discover_server_tools(
        "workspace-1", "target-1", server, request_headers={"x-test": "test"}
    )
    assert [tool.name for tool in tools] == ["good"]
    assert tools[0].input_schema == {"enum": ["x" * 501]}
    assert code == "MCP_DISCOVERY_INVALID_RESPONSE"
    assert "TOOL_SCHEMA_REJECTED" in error and "bad-a" in error and "bad-b" in error
    assert lookup.await_count == 2
    for call in lookup.call_args_list:
        assert call.kwargs["server_id"] == "server-1"
        assert call.kwargs["bypass_cache"] is True
    assert disable.await_count == 1
    args, kwargs = disable.call_args
    assert args[:2] == ("workspace-1", "target-1")
    assert [(tool.name, tool.enabled) for tool in args[2]] == [("bad-a", False), ("bad-b", False)]
    assert kwargs == {
        "server_id": "server-1",
        "target_type": "kubernetes",
        "scope_type": "target",
        "remove_disabled": False,
    }
