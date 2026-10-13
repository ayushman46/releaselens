"""Postgres-backed token buckets (proposed ADR "rate limiting without Redis").

One row per bucket key, updated by a single ``INSERT … ON CONFLICT DO UPDATE``. The row lock
taken by the UPSERT serialises concurrent requests for the same key across every API
process, so the decision is exact without any coordination service:

* tokens refill continuously at ``refill_per_s`` up to ``capacity`` (the burst size);
* a request is allowed when at least one whole token is available, and consumes it;
* a refused request consumes nothing, so a client that keeps hammering is not locked out
  for longer than ``1 / refill_per_s`` after it stops.

The database clock (``clock_timestamp()``) is the only clock involved, so API processes
with skewed clocks agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from rl_db.models.billing import RateLimitBucket
from rl_db.repositories._tenancy import cross_tenant

MAX_BUCKET_KEY_LEN = 255

_TAKE = text(
    """
    INSERT INTO rate_limit_buckets AS b (bucket_key, tokens, allowed, updated_at)
    VALUES (:key, CAST(:capacity AS double precision) - 1, true, clock_timestamp())
    ON CONFLICT (bucket_key) DO UPDATE SET
        tokens = CASE
            WHEN LEAST(:capacity, b.tokens + GREATEST(0, EXTRACT(EPOCH FROM
                     clock_timestamp() - b.updated_at)) * :rate) >= 1
            THEN LEAST(:capacity, b.tokens + GREATEST(0, EXTRACT(EPOCH FROM
                     clock_timestamp() - b.updated_at)) * :rate) - 1
            ELSE LEAST(:capacity, b.tokens + GREATEST(0, EXTRACT(EPOCH FROM
                     clock_timestamp() - b.updated_at)) * :rate)
        END,
        allowed = LEAST(:capacity, b.tokens + GREATEST(0, EXTRACT(EPOCH FROM
                     clock_timestamp() - b.updated_at)) * :rate) >= 1,
        updated_at = clock_timestamp()
    RETURNING allowed, tokens
    """
)


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after_s: int  # 0 when allowed


@cross_tenant
async def take_token(
    session: AsyncSession, key: str, *, capacity: int, refill_per_s: float
) -> RateDecision:
    """Try to take one token from bucket ``key``. The caller commits (immediately: the
    decision must count even if the request it guards fails later)."""
    if capacity < 1 or refill_per_s <= 0:
        raise ValueError("capacity must be >= 1 and refill_per_s > 0")
    if not key or len(key) > MAX_BUCKET_KEY_LEN:
        raise ValueError(f"bucket key must be 1..{MAX_BUCKET_KEY_LEN} characters")
    row = (
        await session.execute(
            _TAKE, {"key": key, "capacity": float(capacity), "rate": float(refill_per_s)}
        )
    ).one()
    tokens = float(row.tokens)
    if row.allowed:
        return RateDecision(allowed=True, remaining=max(0, math.floor(tokens)), retry_after_s=0)
    wait = (1.0 - tokens) / refill_per_s
    return RateDecision(allowed=False, remaining=0, retry_after_s=max(1, math.ceil(wait)))


@cross_tenant
async def delete_idle_buckets(session: AsyncSession, *, idle_for: timedelta) -> int:
    """Drop buckets untouched for ``idle_for`` (they would be full again anyway)."""
    result = await session.execute(
        delete(RateLimitBucket).where(RateLimitBucket.updated_at < func.now() - idle_for)
    )
    return int(getattr(result, "rowcount", 0))
