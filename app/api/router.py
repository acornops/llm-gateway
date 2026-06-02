from fastapi import APIRouter

from app.api.handlers_health import router as health_router
from app.api.handlers_llm_stream import router as llm_router
from app.api.handlers_mcp_admin import router as mcp_admin_router
from app.api.handlers_tool_call import router as tool_router

api_router = APIRouter()

api_router.include_router(health_router, tags=["health"])
api_router.include_router(llm_router, prefix="/llm", tags=["llm"])
api_router.include_router(tool_router, prefix="/mcp", tags=["mcp"])
api_router.include_router(mcp_admin_router, prefix="/internal/mcp", tags=["internal-mcp"])
