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
    server_id: str | None,
) -> tuple[McpServer, Tool | None]:
    """Resolve a server-qualified tool registration."""
    legacy_existing = None
    if scope_type == "agent" and server_id is None:
        raise ValueError("Agent-owned MCP tools require server_id")

    server = None
    if server_id:
        try:
            normalized_server_id = uuid.UUID(server_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("MCP server ID is invalid") from exc
        server = (
            await session.execute(
                select(McpServer).where(
                    McpServer.id == normalized_server_id,
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == scope_type,
                    McpServer.target_id == target_id,
                    McpServer.target_type == target_type,
                )
            )
        ).scalars().first()
    if server is None and scope_type == "target":
        destination_server = (
            await session.execute(
                select(McpServer).where(
                    McpServer.workspace_id == workspace_id,
                    McpServer.scope_type == scope_type,
                    McpServer.target_id == target_id,
                    McpServer.server_url == mcp_server_url,
                )
            )
        ).scalars().first()
        if destination_server is not None and destination_server.target_type != target_type:
            raise ValueError(
                "MCP server is already registered for "
                f"target_type={destination_server.target_type}; "
                f"cannot update through target_type={target_type}"
            )
        server = destination_server
    if server is None:
        if server_id:
            raise ValueError("MCP server must exist before its tools are registered")
        server = McpServer(
            workspace_id=workspace_id,
            scope_type=scope_type,
            target_id=target_id,
            target_type=target_type,
            server_name=(
                "legacy-"
                + uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{workspace_id}:{target_id}:{mcp_server_url}",
                ).hex
            ),
            server_url=mcp_server_url,
            enabled=True,
            auth_type="none",
            auth_scope="none",
            connection_status="unknown",
        )
        session.add(server)
        await session.flush()

    existing = legacy_existing
    if existing is None:
        existing = (
            await session.execute(
                select(Tool).where(
                    Tool.server_id == server.id,
                    Tool.tool_name == tool_name,
                )
            )
        ).scalars().first()
    return server, existing
