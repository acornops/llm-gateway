import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config.settings import settings
from app.mcp.approval_receipts import (
    ApprovalReceiptError,
    ApprovalReceiptStore,
    validate_and_claim_approval_receipt,
)
from app.mcp.canonical_json import canonical_json
from app.mcp.registry.models import ApprovalReceiptUse


@pytest.fixture
async def receipt_store(tmp_path):
    store = ApprovalReceiptStore(f"sqlite+aiosqlite:///{tmp_path / 'receipts.db'}")
    async with store.engine.begin() as connection:
        await connection.run_sync(ApprovalReceiptUse.__table__.create)
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.anyio
async def test_cleanup_does_not_reopen_a_receipt_accepted_with_clock_leeway(
    receipt_keys, receipt_store, monkeypatch
):
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store", receipt_store)
    monkeypatch.setattr(settings, "AUTH_CLOCK_SKEW_SEC", 30)
    now = datetime.now(UTC)
    request = _request()
    receipt = _receipt(
        receipt_keys, request, iat=now - timedelta(seconds=61), exp=now - timedelta(seconds=1)
    )
    await validate_and_claim_approval_receipt(receipt, request)
    await receipt_store.cleanup_expired()
    with pytest.raises(ApprovalReceiptError) as raised:
        await validate_and_claim_approval_receipt(receipt, request)
    assert raised.value.code == "MCP_APPROVAL_RECEIPT_REPLAYED"


@pytest.mark.anyio
@pytest.mark.parametrize("skew", [0, 30])
async def test_cleanup_preserves_existing_rows_until_clock_retention_ends(
    receipt_store, monkeypatch, skew
):
    now = datetime(2030, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("app.mcp.approval_receipts.datetime", Clock)
    monkeypatch.setattr(settings, "AUTH_CLOCK_SKEW_SEC", skew)
    # One leeway interval plus two clocks each offset by one interval.
    cutoff = now - timedelta(seconds=3 * skew)
    for jti, expiration in [("retained", cutoff + timedelta(seconds=1)), ("expired", cutoff)]:
        assert await receipt_store.claim(jti, expiration)
    await receipt_store.cleanup_expired()
    assert not await receipt_store.claim("retained", now)
    assert await receipt_store.claim("expired", now)


@pytest.mark.anyio
async def test_concurrent_receipt_claims_have_one_winner(receipt_store):
    expiration = datetime.now(UTC) + timedelta(seconds=60)
    claims = await asyncio.gather(*(receipt_store.claim("same-jti", expiration) for _ in range(8)))
    assert claims.count(True) == 1
    assert claims.count(False) == 7


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
async def test_dynamic_target_route_receipt_hashes_the_approved_routing_selector(
    receipt_keys, monkeypatch: pytest.MonkeyPatch
):
    import hashlib

    request = _request({"service": "api"})
    approved_arguments = {"service": "api", "target_id": "vm-1"}
    receipt = _receipt(
        receipt_keys,
        request,
        arguments_digest=hashlib.sha256(
            canonical_json(approved_arguments).encode()
        ).hexdigest(),
    )
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr("app.mcp.approval_receipts.approval_receipt_store.claim", claim)

    await validate_and_claim_approval_receipt(receipt, request, approved_arguments)

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
