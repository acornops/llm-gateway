import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.registry.models import McpServer, Tool


async def resolve_tool_registration(
    session: AsyncSession,
    *,
    tool_name: str,
    mcp_server_url: str,
    workspace_id: str,
    scope_type: str,
    target_id: str,
    target_type: str,
    server_id: str,
) -> tuple[McpServer, Tool | None]:
    """Resolve a server-qualified tool registration."""
    try:
        normalized_server_id = uuid.UUID(server_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("MCP server ID is invalid") from exc
    server = (
        await session.execute(
            select(McpServer).where(
                McpServer.id == normalized_server_id,
                McpServer.workspace_id == workspace_id,
            )
        )
    ).scalars().first()
    if server is None:
        raise ValueError("MCP server must exist before its tools are registered")
    if (
        server.scope_type != scope_type
        or server.target_id != target_id
        or server.target_type != target_type
        or server.server_url != mcp_server_url
    ):
        raise ValueError("MCP server destination does not match tool registration")

    existing = (
        await session.execute(
            select(Tool).where(
                Tool.server_id == server.id,
                Tool.tool_name == tool_name,
            )
        )
    ).scalars().first()
    return server, existing
