"""FastAPI adapters for durable MCP lifecycle guards."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from inspect import signature
from typing import Any

from fastapi import HTTPException

from app.api.mcp_admin_validation import registry_destination
from app.mcp.identity import canonical_mcp_server_id
from app.mcp.lifecycle import (
    McpCredentialTransitioningError,
    McpDestination,
    McpLifecycleEpochChangedError,
    McpLifecycleFencedError,
    McpLifecycleServerNotFoundError,
    McpUserLifecycleStaleError,
    mcp_lifecycle_store,
)


def lifecycle_fenced_http_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "MCP_LIFECYCLE_FENCED",
            "message": "MCP lifecycle teardown is in progress for this destination.",
            "retryable": False,
        },
    )


def user_lifecycle_stale_http_error(
    message: str = "The workspace membership generation is no longer active.",
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "MCP_USER_LIFECYCLE_STALE",
            "message": message,
            "retryable": False,
        },
    )


async def assert_guarded_user_active(
    workspace_id: str,
    user_id: str,
    membership_generation: int | None,
) -> object | None:
    """Validate one user generation while the caller holds its server guard."""

    try:
        return await mcp_lifecycle_store.assert_user_active(
            workspace_id,
            user_id,
            membership_generation,
        )
    except McpUserLifecycleStaleError as exc:
        raise user_lifecycle_stale_http_error() from exc


@asynccontextmanager
async def guarded_destination_operation(destination: McpDestination) -> AsyncIterator[None]:
    try:
        async with mcp_lifecycle_store.destination_operation(destination):
            yield
    except McpLifecycleFencedError as exc:
        raise lifecycle_fenced_http_error() from exc


@asynccontextmanager
async def guarded_workspace_mutation(workspace_id: str) -> AsyncIterator[None]:
    """Hold short workspace authority for lifecycle-sensitive persistence."""

    try:
        async with mcp_lifecycle_store.workspace_mutation_lock(workspace_id):
            await mcp_lifecycle_store.assert_workspace_not_fenced(workspace_id)
            yield
    except McpLifecycleFencedError as exc:
        raise lifecycle_fenced_http_error() from exc


async def assert_guarded_workspace_active(workspace_id: str) -> None:
    async with guarded_workspace_mutation(workspace_id):
        return


@asynccontextmanager
async def guarded_server_operation(
    workspace_id: str,
    server_id: str,
    *,
    expected_credential_epoch: int | None = None,
    allow_transitioning: bool = False,
) -> AsyncIterator[object]:
    try:
        async with mcp_lifecycle_store.server_operation(
            workspace_id,
            server_id,
            expected_credential_epoch=expected_credential_epoch,
            allow_transitioning=allow_transitioning,
        ) as server:
            yield server
    except McpLifecycleFencedError as exc:
        raise lifecycle_fenced_http_error() from exc
    except McpLifecycleEpochChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CREDENTIAL_EPOCH_CHANGED",
                "message": "MCP credential configuration changed; retry the request.",
                "retryable": True,
            },
        ) from exc
    except McpCredentialTransitioningError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_CREDENTIAL_TRANSITIONING",
                "message": "Credential ownership is being updated. Retry later.",
            },
        ) from exc
    except McpLifecycleServerNotFoundError as exc:
        raise HTTPException(status_code=404, detail="MCP server not found") from exc


def guarded_server_mutation(*, allow_transitioning: bool = False):
    """Decorate an internal handler whose server/workspace are route-bound.

    The workspace may be a direct handler argument or a ``workspace_id`` field
    on its Pydantic request body. ``functools.wraps`` preserves the FastAPI
    signature and direct unit-test call behavior.
    """

    def decorate(function: Callable[..., Any]):
        function_signature = signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = function_signature.bind_partial(*args, **kwargs)
            server_id = bound.arguments.get("server_id")
            workspace_id = bound.arguments.get("workspace_id")
            request = bound.arguments.get("request")
            if workspace_id is None and request is not None:
                workspace_id = getattr(request, "workspace_id", None)
            if not isinstance(server_id, str) or not isinstance(workspace_id, str):
                raise RuntimeError("guarded MCP handler lacks workspace or server identity")
            canonical_server_id = canonical_mcp_server_id(server_id)
            bound.arguments["server_id"] = canonical_server_id
            async with guarded_server_operation(
                workspace_id,
                canonical_server_id,
                allow_transitioning=allow_transitioning,
            ):
                return await function(*bound.args, **bound.kwargs)

        return wrapped

    return decorate


def guarded_destination_read():
    """Fence a destination-scoped catalog read used for run admission."""

    def decorate(function: Callable[..., Any]):
        function_signature = signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = function_signature.bind_partial(*args, **kwargs)
            workspace_id = bound.arguments.get("workspace_id")
            scope_type = bound.arguments.get("scope_type", "target")
            agent_id = bound.arguments.get("agent_id")
            target_id = bound.arguments.get("target_id")
            target_type = bound.arguments.get("target_type")
            if not isinstance(workspace_id, str) or not isinstance(scope_type, str):
                raise RuntimeError("guarded MCP handler lacks destination identity")
            destination_id, destination_target_type = registry_destination(
                scope_type, target_id, target_type, agent_id
            )
            destination = McpDestination(
                workspace_id=workspace_id,
                scope_type=scope_type,
                destination_id=destination_id,
                target_type=destination_target_type,
            )
            async with guarded_destination_operation(destination):
                return await function(*args, **kwargs)

        return wrapped

    return decorate
