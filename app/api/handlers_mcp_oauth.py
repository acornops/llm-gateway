"""Internal MCP OAuth orchestration."""

from __future__ import annotations

from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Response

from app.api.handlers_mcp_connections import (
    _check_mutation_rate_limit,
)
from app.api.mcp_admin_helpers import _discover_server_tools, merge_connection_discovery
from app.api.mcp_admin_schemas import (
    McpOAuthCompleteRequest,
    McpOAuthCompleteResponse,
    McpOAuthIssuerCandidateResponse,
    McpOAuthPrepareRequest,
    McpOAuthPrepareResponse,
    McpOAuthStartRequest,
    McpOAuthStartResponse,
)
from app.api.mcp_admin_validation import registered_server_request_context
from app.api.mcp_connection_responses import connection_response
from app.auth.service_token import require_admin_service_token
from app.mcp.connections import ConnectionOwner, mcp_connection_store
from app.mcp.header_policy import build_mcp_request_headers
from app.mcp.oauth.errors import McpOAuthError
from app.mcp.oauth.service import (
    complete_authorization,
    prepare_authorization,
    start_authorization,
)
from app.mcp.registry.store import mcp_server_registry
from app.mcp.remote_policy import require_remote_mcp_enabled
from app.mcp.tool_definition_policy import McpToolDefinitionConflictError
from app.observability.metrics import (
    GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL,
)

internal_router = APIRouter()
logger = structlog.get_logger()

_MCP_VERIFICATION_MESSAGES = {
    "MCP_AUTHENTICATION_REJECTED": "The MCP endpoint rejected the OAuth credential.",
    "MCP_DISCOVERY_INVALID_RESPONSE": "The MCP endpoint returned an invalid discovery response.",
    "MCP_DISCOVERY_RESPONSE_TOO_LARGE": "The MCP discovery response exceeded the allowed size.",
    "MCP_DISCOVERY_TIMEOUT": "The MCP endpoint timed out during verification.",
    "MCP_EGRESS_BLOCKED": "The MCP endpoint was blocked by the outbound network policy.",
    "MCP_ENDPOINT_NOT_FOUND": "The MCP endpoint returned Not Found during verification.",
    "MCP_ENDPOINT_UNAVAILABLE": "The MCP endpoint was unavailable during verification.",
    "MCP_PROTOCOL_ERROR": "The MCP endpoint returned an incompatible protocol response.",
    "MCP_TOOL_DISCOVERY_FAILED": "The MCP endpoint could not complete tool discovery.",
}


async def _verify_completed_authorization(flow, bundle, connection):
    """Verify the exchanged token while the OAuth service holds the owner lock."""

    try:
        server = await _oauth_server(flow.workspace_id, flow.server_id)
        destination_id, _registry_scope, platform_headers = registered_server_request_context(
            flow.workspace_id, server
        )
        headers = build_mcp_request_headers(
            server,
            bundle.access_token,
            platform_headers=platform_headers,
        )
        tools, discovery_error, discovery_error_code = await _discover_server_tools(
            flow.workspace_id,
            destination_id,
            server,
            request_headers=headers,
        )
        if discovery_error is not None:
            verification_code = discovery_error_code or "MCP_TOOL_DISCOVERY_FAILED"
            verification_status = (
                "reauthorization_required"
                if verification_code == "MCP_AUTHENTICATION_REJECTED"
                else "error"
            )
            await mcp_connection_store.set_state(
                connection,
                verification_status,
                error_code=verification_code,
                oauth_scopes=bundle.scopes,
                oauth_token_expires_at=bundle.expires_at,
                oauth_refresh_capable=bool(bundle.refresh_token),
            )
            GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
                stage="verify",
                method=flow.registration_method,
                outcome=verification_code.lower(),
            ).inc()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": verification_code,
                    "message": _MCP_VERIFICATION_MESSAGES.get(
                        verification_code,
                        "OAuth succeeded, but the MCP endpoint could not be verified.",
                    ),
                    "retryable": verification_code
                    in {
                        "MCP_DISCOVERY_TIMEOUT",
                        "MCP_ENDPOINT_UNAVAILABLE",
                    },
                    "return_path": flow.return_path,
                    "workspace_id": flow.workspace_id,
                    "server_id": flow.server_id,
                },
            )
        verified_tool_names = await merge_connection_discovery(server, tools)
        connected = await mcp_connection_store.set_state(
            connection,
            "connected",
            verified_tool_names=verified_tool_names,
            oauth_scopes=bundle.scopes,
            oauth_token_expires_at=bundle.expires_at,
            oauth_refresh_capable=bool(bundle.refresh_token),
        )
        if connected is None:
            raise ValueError("OAuth connection disappeared during verification")
        return connected
    except McpToolDefinitionConflictError:
        await mcp_connection_store.set_state(
            connection,
            "error",
            error_code="MCP_PROTOCOL_ERROR",
        )
        GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
            stage="verify",
            method=flow.registration_method,
            outcome="mcp_protocol_error",
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_PROTOCOL_ERROR",
                "message": (
                    "The MCP server returned a tool definition that conflicts "
                    "with the reviewed installation."
                ),
                "retryable": False,
                "return_path": flow.return_path,
                "workspace_id": flow.workspace_id,
                "server_id": flow.server_id,
            },
        ) from None
    except HTTPException:
        raise
    except Exception:
        await mcp_connection_store.set_state(
            connection,
            "error",
            error_code="MCP_OAUTH_VERIFICATION_FAILED",
        )
        raise


def _oauth_http_error(error: McpOAuthError) -> HTTPException:
    detail: dict[str, object] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.return_path is not None:
        detail["return_path"] = error.return_path
    if error.workspace_id is not None:
        detail["workspace_id"] = error.workspace_id
    if error.server_id is not None:
        detail["server_id"] = error.server_id
    return HTTPException(
        status_code=error.status_code,
        detail=detail,
    )


def _resource_origin(resource: str) -> str:
    parsed = urlparse(resource)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _oauth_server(workspace_id: str, server_id: str):
    server = await mcp_server_registry.get_server_for_workspace(workspace_id, server_id)
    if server is None or server.auth_type != "oauth" or server.credential_mode != "individual":
        raise HTTPException(status_code=404, detail="OAuth MCP server not found")
    return server


@internal_router.post(
    "/servers/{server_id}/connections/{owner_id}/oauth/prepare",
    response_model=McpOAuthPrepareResponse,
)
async def prepare_mcp_oauth(
    request: McpOAuthPrepareRequest,
    response: Response,
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpOAuthPrepareResponse:
    response.headers["Cache-Control"] = "no-store"
    if request.owner_id != owner_id:
        raise HTTPException(status_code=422, detail="Connection owner does not match route")
    require_remote_mcp_enabled()
    server = await _oauth_server(request.workspace_id, server_id)
    await _check_mutation_rate_limit(
        request.workspace_id,
        server_id,
        ConnectionOwner("user", owner_id),
    )
    try:
        handle, preparation = await prepare_authorization(
            server=server,
            workspace_id=request.workspace_id,
            owner_id=owner_id,
            browser_binding_hash=request.browser_binding_hash,
            return_path=request.return_path,
        )
    except McpOAuthError as error:
        GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
            stage="prepare",
            method="none",
            outcome=error.code.lower(),
        ).inc()
        raise _oauth_http_error(error) from error
    GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
        stage="prepare",
        method="automatic",
        outcome="success",
    ).inc()
    return McpOAuthPrepareResponse(
        preparation_handle=handle,
        resource_origin=_resource_origin(preparation.resource),
        candidates=[
            McpOAuthIssuerCandidateResponse.model_validate(candidate.model_dump())
            for candidate in preparation.candidates
        ],
        issuer_selection_required=len(preparation.candidates) > 1,
    )


@internal_router.post(
    "/servers/{server_id}/connections/{owner_id}/oauth/start",
    response_model=McpOAuthStartResponse,
)
async def start_mcp_oauth(
    request: McpOAuthStartRequest,
    response: Response,
    server_id: str = Path(...),
    owner_id: str = Path(..., min_length=1),
    _token_ok: None = Depends(require_admin_service_token),
) -> McpOAuthStartResponse:
    response.headers["Cache-Control"] = "no-store"
    if request.owner_id != owner_id:
        raise HTTPException(status_code=422, detail="Connection owner does not match route")
    await _oauth_server(request.workspace_id, server_id)
    await _check_mutation_rate_limit(
        request.workspace_id,
        server_id,
        ConnectionOwner("user", owner_id),
    )
    try:
        authorization_url, _state, metadata_changed = await start_authorization(
            preparation_handle=request.preparation_handle,
            workspace_id=request.workspace_id,
            server_id=server_id,
            owner_id=owner_id,
            browser_binding_hash=request.browser_binding_hash,
            issuer=request.issuer,
            consent_granted=request.consent_granted,
        )
    except McpOAuthError as error:
        GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
            stage="start",
            method="automatic",
            outcome=error.code.lower(),
        ).inc()
        raise _oauth_http_error(error) from error
    GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
        stage="start",
        method="automatic",
        outcome="success",
    ).inc()
    return McpOAuthStartResponse(
        authorization_url=authorization_url,
        metadata_changed=metadata_changed,
    )


@internal_router.post(
    "/oauth/complete",
    response_model=McpOAuthCompleteResponse,
)
async def complete_mcp_oauth(
    request: McpOAuthCompleteRequest,
    response: Response,
    _token_ok: None = Depends(require_admin_service_token),
) -> McpOAuthCompleteResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        flow, _bundle, connection = await complete_authorization(
            code=request.code,
            state=request.state,
            issuer=request.issuer,
            provider_error=request.provider_error,
            owner_id=request.owner_id,
            browser_binding_hash=request.browser_binding_hash,
            verify_connection=_verify_completed_authorization,
        )
        server = await _oauth_server(flow.workspace_id, flow.server_id)
    except McpOAuthError as error:
        GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
            stage="complete",
            method="automatic",
            outcome=error.code.lower(),
        ).inc()
        raise _oauth_http_error(error) from error
    except HTTPException:
        raise
    except Exception as error:
        logger.warning(
            "mcp_oauth_verification_failed",
            exception_type=error.__class__.__name__,
        )
        GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
            stage="complete",
            method="automatic",
            outcome="verification_failed",
        ).inc()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_OAUTH_VERIFICATION_FAILED",
                "message": "OAuth succeeded, but the MCP server could not be verified.",
                "retryable": False,
                **(
                    {
                        "return_path": flow.return_path,
                        "workspace_id": flow.workspace_id,
                        "server_id": flow.server_id,
                    }
                    if "flow" in locals()
                    else {}
                ),
            },
        ) from error
    GATEWAY_MCP_OAUTH_OPERATIONS_TOTAL.labels(
        stage="complete",
        method=flow.registration_method,
        outcome="success",
    ).inc()
    return McpOAuthCompleteResponse(
        connection=connection_response(server, connection),
        return_path=flow.return_path,
        workspace_id=flow.workspace_id,
        server_id=flow.server_id,
    )
