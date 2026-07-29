"""Stable, sanitized MCP OAuth failures."""

from __future__ import annotations


class McpOAuthError(ValueError):
    """A protocol or policy failure safe to map to a stable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        return_path: str | None = None,
        workspace_id: str | None = None,
        server_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.return_path = return_path
        self.workspace_id = workspace_id
        self.server_id = server_id


def oauth_error(
    code: str,
    message: str,
    *,
    status_code: int = 400,
    retryable: bool = False,
    return_path: str | None = None,
    workspace_id: str | None = None,
    server_id: str | None = None,
) -> McpOAuthError:
    return McpOAuthError(
        code,
        message,
        status_code=status_code,
        retryable=retryable,
        return_path=return_path,
        workspace_id=workspace_id,
        server_id=server_id,
    )
