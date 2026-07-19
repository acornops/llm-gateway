from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import jwt
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.jwks import jwks_manager
from app.config.settings import settings
from app.mcp.canonical_json import canonical_json
from app.mcp.registry.models import ApprovalReceiptUse


class ApprovalReceiptError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ApprovalReceiptStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def claim(self, jti: str, expires_at: datetime) -> bool:
        async with self.async_session() as session:
            session.add(ApprovalReceiptUse(jti=jti, expires_at=expires_at))
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def cleanup_expired(self) -> None:
        async with self.async_session() as session:
            await session.execute(
                delete(ApprovalReceiptUse).where(ApprovalReceiptUse.expires_at <= datetime.now(UTC))
            )
            await session.commit()

    async def close(self) -> None:
        await self.engine.dispose()


approval_receipt_store = ApprovalReceiptStore(settings.DATABASE_URL)


def _required_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ApprovalReceiptError("MCP_APPROVAL_RECEIPT_INVALID", "Approval receipt is invalid.")
    return value


async def validate_and_claim_approval_receipt(receipt: str, request) -> None:
    try:
        header = jwt.get_unverified_header(receipt)
        if header.get("alg") != "RS256" or header.get("typ") != "acornops-approval+jwt":
            raise ApprovalReceiptError(
                "MCP_APPROVAL_RECEIPT_INVALID", "Approval receipt is invalid."
            )
        signing_key = await jwks_manager.get_signing_key(receipt)
        payload = jwt.decode(
            receipt,
            signing_key,
            algorithms=["RS256"],
            audience="acornops-mcp-approval",
            issuer=settings.AUTH_ISSUER,
            leeway=settings.AUTH_CLOCK_SKEW_SEC,
            options={"require": ["exp", "iat", "jti", "sub"]},
        )
    except ApprovalReceiptError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_EXPIRED", "Approval receipt has expired."
        ) from exc
    except jwt.PyJWTError as exc:
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_INVALID", "Approval receipt is invalid."
        ) from exc
    except Exception as exc:
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_INVALID", "Approval receipt is invalid."
        ) from exc

    jti = _required_string(payload, "jti")
    approval_id = _required_string(payload, "approval_id")
    if payload.get("sub") != f"approval:{approval_id}":
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_MISMATCH", "Approval receipt does not match this call."
        )
    if request.tool_ref is None or not request.tool_call_id:
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_MISMATCH", "Approval receipt does not match this call."
        )
    try:
        arguments_digest = hashlib.sha256(
            canonical_json(request.arguments).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_INVALID", "Approval receipt is invalid."
        ) from exc
    expected = {
        "run_id": request.run_id,
        "workspace_id": request.workspace_id,
        "tool_call_id": request.tool_call_id,
        "tool_alias": request.tool,
        "server_id": request.tool_ref.server_id,
        "server_tool_name": request.tool_ref.tool_name,
        "arguments_digest": arguments_digest,
    }
    if any(_required_string(payload, key) != value for key, value in expected.items()):
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_MISMATCH", "Approval receipt does not match this call."
        )
    exp = payload.get("exp")
    issued_at = payload.get("iat")
    if (
        not isinstance(exp, (int, float))
        or not isinstance(issued_at, (int, float))
        or exp <= issued_at
        or exp - issued_at > 60
    ):
        raise ApprovalReceiptError("MCP_APPROVAL_RECEIPT_INVALID", "Approval receipt is invalid.")
    if not await approval_receipt_store.claim(jti, datetime.fromtimestamp(exp, UTC)):
        raise ApprovalReceiptError(
            "MCP_APPROVAL_RECEIPT_REPLAYED", "Approval receipt was already used."
        )


async def approval_receipt_cleanup_loop() -> None:
    while True:
        await approval_receipt_store.cleanup_expired()
        await asyncio.sleep(60)
