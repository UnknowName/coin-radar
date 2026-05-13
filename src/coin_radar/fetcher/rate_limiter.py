from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """令牌桶限流器：控制每秒允许的请求数，支持突发流量"""

    def __init__(self, rate: float, capacity: int) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # 计算需要等待的时间
            wait_time = (1.0 - self._tokens) / self._rate
        await asyncio.sleep(wait_time)
        async with self._lock:
            self._refill()
            self._tokens -= 1.0
