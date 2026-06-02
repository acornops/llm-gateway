import time

from fastapi import HTTPException
from redis.asyncio import Redis

from app.config.settings import settings


class RateLimiter:
    """
    Distributed rate limiter using Redis.
    Uses a fixed-window counter approach.
    """

    def __init__(self, redis_url: str):
        self.redis = Redis.from_url(redis_url)

    async def check_rate_limit(self, key: str, limit: int, window: int):
        """
        Checks if a key has exceeded its rate limit.
        """
        current_time = int(time.time())
        window_key = f"rate_limit:{key}:{current_time // window}"

        count = await self.redis.incr(window_key)
        if count == 1:
            await self.redis.expire(window_key, window)

        if count > limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")


rate_limiter = None
if settings.REDIS_URL:
    rate_limiter = RateLimiter(settings.REDIS_URL)
