import pytest
from fastapi import HTTPException

from app.resilience.rate_limit import RateLimiter


class FakeRedis:
    def __init__(self, counts: list[int]):
        self._counts = list(counts)
        self.expire_calls: list[tuple[str, int]] = []
        self.incr_calls: list[str] = []

    async def incr(self, key: str) -> int:
        self.incr_calls.append(key)
        return self._counts.pop(0)

    async def expire(self, key: str, window: int) -> None:
        self.expire_calls.append((key, window))


@pytest.mark.anyio
async def test_rate_limiter_sets_expiry_on_first_hit(monkeypatch: pytest.MonkeyPatch):
    fake_redis = FakeRedis([1])
    monkeypatch.setattr("app.resilience.rate_limit.Redis.from_url", lambda url: fake_redis)
    monkeypatch.setattr("app.resilience.rate_limit.time.time", lambda: 30)

    limiter = RateLimiter("redis://example")
    await limiter.check_rate_limit("tenant", limit=5, window=10)

    assert fake_redis.incr_calls == ["rate_limit:tenant:3"]
    assert fake_redis.expire_calls == [("rate_limit:tenant:3", 10)]


@pytest.mark.anyio
async def test_rate_limiter_raises_when_limit_is_exceeded(monkeypatch: pytest.MonkeyPatch):
    fake_redis = FakeRedis([6])
    monkeypatch.setattr("app.resilience.rate_limit.Redis.from_url", lambda url: fake_redis)
    monkeypatch.setattr("app.resilience.rate_limit.time.time", lambda: 30)

    limiter = RateLimiter("redis://example")

    with pytest.raises(HTTPException, match="Rate limit exceeded") as exc_info:
        await limiter.check_rate_limit("tenant", limit=5, window=10)

    assert exc_info.value.status_code == 429
    assert fake_redis.expire_calls == []
