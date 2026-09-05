"""Shared MCP lifecycle identities and typed failure states."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.mcp.registry.models import McpServer


class McpLifecycleError(RuntimeError):
    pass


class McpLifecycleFencedError(McpLifecycleError):
    pass


class McpLifecycleEpochChangedError(McpLifecycleError):
    pass


class McpCredentialTransitioningError(McpLifecycleError):
    pass


class McpLifecycleServerNotFoundError(McpLifecycleError):
    pass


class McpUserLifecycleStaleError(McpLifecycleError):
    pass


class McpUserLifecycleConflictError(McpLifecycleError):
    pass


@dataclass(frozen=True)
class McpDestination:
    workspace_id: str
    scope_type: str
    destination_id: str
    target_type: str | None = None

    def __post_init__(self) -> None:
        if self.scope_type == "agent":
            if self.target_type is not None:
                raise ValueError("agent MCP destinations do not accept target_type")
            return
        if self.scope_type != "target" or not self.target_type:
            raise ValueError("target MCP destinations require target_type")

    @classmethod
    def from_server(cls, server: McpServer) -> McpDestination:
        scope_type = getattr(server, "scope_type", "target")
        if scope_type == "agent":
            destination_id = getattr(server, "agent_id", None)
            target_type = None
        else:
            destination_id = getattr(server, "target_id", None)
            target_type = getattr(server, "target_type", None)
        if not destination_id:
            raise ValueError("MCP server has no destination identity")
        return cls(
            workspace_id=server.workspace_id,
            scope_type=scope_type,
            destination_id=destination_id,
            target_type=target_type,
        )

    @property
    def fence_key(self) -> str:
        return json.dumps(
            [self.scope_type, self.target_type or "", self.destination_id],
            separators=(",", ":"),
        )
