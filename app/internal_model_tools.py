INTERNAL_TOOL_NAME_PREFIX = "_acornops_"
INTERNAL_MODEL_ONLY_TOOLS = frozenset({"_acornops_load_skill"})


def is_internal_model_only_tool_name(tool_name: str) -> bool:
    return tool_name in INTERNAL_MODEL_ONLY_TOOLS


def is_reserved_internal_tool_name(tool_name: str) -> bool:
    return tool_name in INTERNAL_MODEL_ONLY_TOOLS or tool_name.startswith(
        INTERNAL_TOOL_NAME_PREFIX
    )
