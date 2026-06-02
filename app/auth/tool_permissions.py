def is_tool_permitted(tool_name: str, allowed_tools: list[str]) -> bool:
    allowed_tool_names = set(allowed_tools)
    return "*" in allowed_tool_names or tool_name in allowed_tool_names


def disallowed_tools(requested_tool_names: list[str], allowed_tools: list[str]) -> list[str]:
    allowed_tool_names = set(allowed_tools)
    allow_all_tools = "*" in allowed_tool_names
    return [
        tool_name
        for tool_name in requested_tool_names
        if not allow_all_tools and tool_name not in allowed_tool_names
    ]
