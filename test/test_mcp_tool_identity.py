from app.mcp.tool_identity import model_tool_alias


def test_model_tool_alias_is_deterministic_and_server_qualified() -> None:
    first = model_tool_alias("11111111-1111-1111-1111-111111111111", "records.list")
    repeated = model_tool_alias("11111111-1111-1111-1111-111111111111", "records.list")
    other_server = model_tool_alias("22222222-2222-2222-2222-222222222222", "records.list")

    assert first == repeated
    assert first != other_server
    assert first.startswith("m_11111111111111111111111111111111_records_list_")
    assert len(first) <= 64
