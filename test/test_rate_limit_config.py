import pytest

from app.api.handlers_llm_stream import stream_generation
from app.api.handlers_tool_call import ToolCallRequest, execute_tool_call
from app.auth.claims import Permissions, TokenClaims
from app.auth.jwt_validator import TokenContext
from app.llm.service import Message, NormalizedLLMRequest


class FakeRateLimiter:
    def __init__(self):
        self.calls: list[tuple[str, int, int]] = []

    async def check_rate_limit(self, key: str, limit: int, window: int):
        self.calls.append((key, limit, window))
        raise RuntimeError("stop after rate-limit check")


def claims() -> TokenClaims:
    return TokenClaims(
        iss="issuer",
        aud="audience",
        iat=1,
        exp=999,
        sub="sub-1",
        user_id="user-1",
        run_id="run-1",
        workspace_id="ws-1",
        target_id="cl-1",
        target_type="kubernetes",
        session_id="session-1",
        permissions=Permissions(),
    )


@pytest.mark.anyio
async def test_llm_rate_limit_uses_configured_values(monkeypatch: pytest.MonkeyPatch):
    limiter = FakeRateLimiter()
    monkeypatch.setattr("app.api.handlers_llm_stream.rate_limiter", limiter)
    monkeypatch.setattr("app.api.handlers_llm_stream.settings.LLM_RATE_LIMIT_PER_WINDOW", 17)
    monkeypatch.setattr("app.api.handlers_llm_stream.settings.RATE_LIMIT_WINDOW_SECONDS", 11)

    request = NormalizedLLMRequest(
        run_id="run-1",
        workspace_id="ws-1",
        target_id="cl-1",
        target_type="kubernetes",
        session_id="session-1",
        provider="openai",
        model="gpt-4.1-mini",
        messages=[Message(role="user", content="hello")],
    )

    with pytest.raises(RuntimeError, match="stop after rate-limit check"):
        await stream_generation(request, claims())

    assert limiter.calls == [("llm:ws-1", 17, 11)]


@pytest.mark.anyio
async def test_tool_rate_limit_uses_configured_values(monkeypatch: pytest.MonkeyPatch):
    limiter = FakeRateLimiter()
    monkeypatch.setattr("app.api.handlers_tool_call.rate_limiter", limiter)
    monkeypatch.setattr("app.api.handlers_tool_call.settings.TOOL_RATE_LIMIT_PER_WINDOW", 23)
    monkeypatch.setattr("app.api.handlers_tool_call.settings.RATE_LIMIT_WINDOW_SECONDS", 13)

    request = ToolCallRequest(
        run_id="run-1",
        workspace_id="ws-1",
        target_id="cl-1",
        target_type="kubernetes",
        tool="list_pods",
        arguments={},
    )

    with pytest.raises(RuntimeError, match="stop after rate-limit check"):
        await execute_tool_call(request, TokenContext(token="run-token", claims=claims()))

    assert limiter.calls == [("tool:ws-1", 23, 13)]
