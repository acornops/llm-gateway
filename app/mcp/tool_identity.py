import hashlib
import re


def model_tool_alias(server_id: str, tool_name: str) -> str:
    """Return a deterministic model-safe alias for one server-qualified tool."""
    server_key = re.sub(r"[^a-fA-F0-9]", "", server_id).lower()[:32]
    readable = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_name).strip("_")[:16] or "tool"
    tool_digest = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:10]
    return f"m_{server_key}_{readable}_{tool_digest}"
