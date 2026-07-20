from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config.settings import settings
from app.mcp.approval_receipts import (
    ApprovalReceiptError,
    validate_and_claim_approval_receipt,
)
from app.mcp.canonical_json import canonical_json


def _request(arguments: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-1",
        workspace_id="workspace-1",
        tool_call_id="call-1",
        tool="restart_workload",
        tool_ref=SimpleNamespace(server_id="server-1", tool_name="restart_workload"),
        arguments=arguments or {"replicas": 2, "namespace": "payments"},
    )


def _receipt(private_key, request: SimpleNamespace, **overrides) -> str:
    import hashlib

    now = datetime.now(UTC)
    payload = {
        "iss": settings.AUTH_ISSUER,
        "aud": "acornops-mcp-approval",
        "sub": "approval:approval-1",
        "jti": "receipt-1",
        "iat": now,
        "exp": now + timedelta(seconds=60),
        "approval_id": "approval-1",
        "run_id": request.run_id,
        "workspace_id": request.workspace_id,
        "tool_call_id": request.tool_call_id,
        "tool_alias": request.tool,
        "server_id": request.tool_ref.server_id,
        "server_tool_name": request.tool_ref.tool_name,
        "arguments_digest": hashlib.sha256(canonical_json(request.arguments).encode()).hexdigest(),
        **overrides,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"typ": "acornops-approval+jwt", "kid": "test-key"},
    )


@pytest.fixture
def receipt_keys(monkeypatch: pytest.MonkeyPatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        "app.mcp.approval_receipts.jwks_manager.get_signing_key",
        AsyncMock(return_value=private_key.public_key()),
    )
    return private_key


@pytest.mark.anyio
async def test_exact_approval_receipt_is_claimed_once(
    receipt_keys, monkeypatch: pytest.MonkeyPatch
):
    request = _request()
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)

    await validate_and_claim_approval_receipt(_receipt(receipt_keys, request), request)

    claim.assert_awaited_once()


@pytest.mark.anyio
async def test_receipt_accepts_formerly_divergent_ieee754_arguments(
    receipt_keys, monkeypatch: pytest.MonkeyPatch
):
    request = _request({"amount": float(1373428634809579000)})
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)

    await validate_and_claim_approval_receipt(_receipt(receipt_keys, request), request)

    assert canonical_json(request.arguments) == '{"amount":1373428634809579000}'
    claim.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "override",
    [
        {"run_id": "wrong-run"},
        {"workspace_id": "wrong-workspace"},
        {"tool_call_id": "wrong-call"},
        {"tool_alias": "wrong-alias"},
        {"server_id": "wrong-server"},
        {"server_tool_name": "wrong-tool"},
        {"arguments_digest": "0" * 64},
    ],
)
async def test_mismatched_approval_receipts_fail_before_claim(receipt_keys, monkeypatch, override):
    request = _request()
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)

    with pytest.raises(ApprovalReceiptError, match="does not match") as raised:
        await validate_and_claim_approval_receipt(
            _receipt(receipt_keys, request, **override), request
        )

    assert raised.value.code == "MCP_APPROVAL_RECEIPT_MISMATCH"
    claim.assert_not_awaited()


@pytest.mark.anyio
async def test_replayed_approval_receipt_is_rejected(receipt_keys, monkeypatch: pytest.MonkeyPatch):
    request = _request()
    monkeypatch.setattr(
        "app.mcp.approval_receipts.approval_receipt_store.claim",
        AsyncMock(return_value=False),
    )

    with pytest.raises(ApprovalReceiptError) as raised:
        await validate_and_claim_approval_receipt(_receipt(receipt_keys, request), request)

    assert raised.value.code == "MCP_APPROVAL_RECEIPT_REPLAYED"


@pytest.mark.anyio
async def test_expired_approval_receipt_is_rejected_before_claim(
    receipt_keys, monkeypatch: pytest.MonkeyPatch
):
    request = _request()
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)

    with pytest.raises(ApprovalReceiptError) as raised:
        await validate_and_claim_approval_receipt(
            _receipt(
                receipt_keys,
                request,
                iat=datetime.now(UTC) - timedelta(seconds=180),
                exp=datetime.now(UTC) - timedelta(seconds=120),
            ),
            request,
        )

    assert raised.value.code == "MCP_APPROVAL_RECEIPT_EXPIRED"
    claim.assert_not_awaited()


@pytest.mark.anyio
async def test_random_approval_receipt_is_rejected_before_claim(
    monkeypatch: pytest.MonkeyPatch,
):
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)

    with pytest.raises(ApprovalReceiptError) as raised:
        await validate_and_claim_approval_receipt("not-a-jwt", _request())

    assert raised.value.code == "MCP_APPROVAL_RECEIPT_INVALID"
    claim.assert_not_awaited()


def test_canonical_json_matches_rfc_8785_ordering_and_number_forms():
    assert canonical_json({"z": 1e-7, "a": [1e-6, -0.0], "€": "ok"}) == (
        '{"a":[0.000001,0],"z":1e-7,"€":"ok"}'
    )
