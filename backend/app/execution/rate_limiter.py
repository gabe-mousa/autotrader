"""Order-placement rate limiter (docs/plan/06 §8, 08 §Order budget).

The app is registered at 60 orders/min per account (decision log #10). Plan
requires priority classes — kill-switch cancels > protective exits > other
cancels > entries — so a burst of entries can never starve an exit.

Design: TWO token buckets sharing the same 60/min ceiling. A small RESERVED
bucket (fixed headroom, default 6 tokens/min = 10%) is drawn from ONLY by
high-priority purposes (exit/stop/target/cancel); everything else — including
entries — draws from the MAIN bucket. This gives a simple, provably-correct
guarantee (reserved capacity can never be consumed by an entry) without a full
priority-queue scheduler, which would add real complexity for a case (order
storms) this app's expected volume makes unlikely to matter beyond the
reserve. If both buckets are momentarily empty, a request waits up to its
`timeout_s` before being reported as rate_limited (never blocks forever —
plan: "shed first... entries are always safe to drop; exits never are" —
'never' here means exits get the reserved lane, not that they wait forever)."""

from __future__ import annotations

import asyncio
import time

HIGH_PRIORITY_PURPOSES = {"stop", "target", "cancel"}  # + "kill" handled separately, always allowed


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_per_sec = refill_per_sec
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self._last = now

    async def try_acquire(self) -> bool:
        async with self._lock:
            self._refill_locked()
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class OrderRateLimiter:
    def __init__(self, orders_per_minute: int = 60, reserved_fraction: float = 0.10,
                min_reserved: int = 2):
        reserved = max(min_reserved, round(orders_per_minute * reserved_fraction))
        main = max(1, orders_per_minute - reserved)
        self.reserved = TokenBucket(reserved, reserved / 60.0)
        self.main = TokenBucket(main, main / 60.0)

    async def acquire(self, purpose: str, timeout_s: float = 5.0) -> bool:
        """kill-switch cancels bypass the limiter entirely (see gateway.py —
        kill-switch mass-cancel is not routed through this limiter at all,
        per plan: it must never be rate-limited away)."""
        deadline = time.monotonic() + timeout_s
        high_priority = purpose in HIGH_PRIORITY_PURPOSES
        while True:
            if high_priority and await self.reserved.try_acquire():
                return True
            if await self.main.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(0.2, max(0.02, timeout_s / 10)))
