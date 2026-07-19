from fastapi import HTTPException

from app.config.settings import settings

REMOTE_MCP_DISABLED_DETAIL = {
    "code": "MCP_REMOTE_DISABLED",
    "message": "Remote MCP discovery and execution are disabled by the operator.",
}


def require_remote_mcp_enabled() -> None:
    if not settings.REMOTE_MCP_ENABLED:
        raise HTTPException(status_code=503, detail=REMOTE_MCP_DISABLED_DETAIL)
