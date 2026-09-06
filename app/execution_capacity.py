"""Control-plane lifecycle and single-dispatch operation fencing for verified runs."""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.config.settings import settings
from app.internal_transport import httpx_tls_kwargs

OWNER_HEADER = "x-acornops-execution-owner"
GENERATION_HEADER = "x-acornops-execution-generation"


@dataclass
class Operation:
    claims: object
    headers: dict
    payload: dict | None
    deadline: float
    dispatched: bool = False


current_operation: ContextVar[Operation | None] = ContextVar("gateway_operation", default=None)


class ExecutionAuthority:
    async def _post(self, run_id, action, payload):
        base = settings.ORCH_BASE_URL.rstrip("/")
        url = f"{base}/internal/v1/runs/{quote(run_id, safe='')}/capacity/{action}"
        try:
            async with httpx.AsyncClient(timeout=5, **httpx_tls_kwargs()) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.ORCH_SERVICE_TOKEN}"},
                )
            if response.status_code >= 400:
                raise HTTPException(
                    response.status_code if response.status_code < 500 else 503,
                    detail={
                        "code": "EXECUTION_AUTHORITY_DENIED",
                        "message": "Run execution is not authorized.",
                    },
                )
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, detail="Execution authority unavailable") from error

    async def authorize(self, claims):
        result = await self._post(claims.run_id, "authorize", {"workspaceId": claims.workspace_id})
        if (
            result.get("status") != "ok"
            or result.get("contractVersion") != 1
            or result.get("capacityEnabled") is not settings.WORKSPACE_CAPACITY_ENABLED
        ):
            raise HTTPException(503, detail="Incompatible execution authority contract")

    @asynccontextmanager
    async def operation(self, claims, headers, timeout_ms):
        await self.authorize(claims)
        timeout_ms = min(max(int(timeout_ms), 1), 3600000)
        payload = None
        if settings.WORKSPACE_CAPACITY_ENABLED:
            owner = headers.get(OWNER_HEADER)
            try:
                generation = int(headers.get(GENERATION_HEADER, "0"))
            except ValueError:
                generation = 0
            if not owner or len(owner) > 200 or generation < 1:
                raise HTTPException(409, detail="Execution owner and generation required")
        operation = Operation(claims, dict(headers), payload, time.monotonic() + timeout_ms / 1000)
        token = current_operation.set(operation)
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                yield
        finally:
            current_operation.reset(token)
            if operation.payload:
                # Finish is idempotent; one bounded call, including cancellation cleanup.
                await self._post(claims.run_id, "operations/finish", operation.payload)


execution_authority = ExecutionAuthority()


async def begin_dispatch():
    """Authorize lifecycle and ownership at the actual upstream dispatch boundary."""
    operation = current_operation.get()
    if operation is None:
        return
    if operation.dispatched:
        raise HTTPException(409, detail="Execution operation may not be replayed")
    operation.dispatched = True
    await execution_authority.authorize(operation.claims)
    if settings.WORKSPACE_CAPACITY_ENABLED and operation.payload is None:
        remaining = int((operation.deadline - time.monotonic()) * 1000)
        if remaining < 1:
            raise HTTPException(409, detail="Provider operation deadline elapsed")
        payload = {
            "ownerId": operation.headers[OWNER_HEADER],
            "generation": int(operation.headers[GENERATION_HEADER]),
            "operationId": uuid.uuid4().hex,
        }
        result = await execution_authority._post(
            operation.claims.run_id, "operations/begin", {**payload, "timeoutMs": remaining}
        )
        if result.get("status") != "ok" or result.get("contractVersion") != 1:
            raise HTTPException(409, detail="Execution operation rejected")
        operation.payload = payload


async def provider_dispatch_hook(request):
    """Fence every actual SDK request, including the first request after adapter setup."""
    await begin_dispatch()


async def provider_response_hook(response):
    """A definitive validation rejection may be corrected with a new operation."""
    if response.status_code == 400:
        await finish_rejected_dispatch()


async def finish_rejected_dispatch():
    """Finish a definitive protocol rejection before any corrected request gets a new ID."""
    operation = current_operation.get()
    if operation is None:
        return
    if operation.payload:
        await execution_authority._post(
            operation.claims.run_id, "operations/finish", operation.payload
        )
        operation.payload = None
    operation.dispatched = False
