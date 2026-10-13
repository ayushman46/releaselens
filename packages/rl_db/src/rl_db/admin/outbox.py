"""Outbox relay queries (cross-tenant: the relay publishes every tenant's messages).

The relay holds the claim transaction open while it produces, and marks rows published
before committing. ``FOR UPDATE SKIP LOCKED`` lets several relays run without publishing
the same row concurrently. A crash after the broker ack but before the commit re-publishes
the row later (at least once), so consumers deduplicate on the ``rl-outbox-id`` header.

A failed row is pushed back (``available_at``) by the caller's backoff and skipped until
then, so rows that keep failing neither hog every batch nor spin in a hot retry loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_db.models.outbox import OutboxEvent
from rl_db.repositories._tenancy import cross_tenant

MAX_ERROR_LEN = 2000


@dataclass(frozen=True, slots=True)
class PendingOutboxEvent:
    id: uuid.UUID
    tenant_id: uuid.UUID
    topic: str
    key: str
    payload: dict[str, Any]
    headers: dict[str, str]
    attempts: int


@cross_tenant
async def claim_unpublished(session: AsyncSession, *, limit: int) -> list[PendingOutboxEvent]:
    """Lock and return the oldest unpublished rows that are due (UUIDv7 ids are
    time-ordered). Rows waiting out a retry backoff are skipped.

    The rows stay locked until the caller's transaction ends.
    """
    stmt = (
        select(
            OutboxEvent.id,
            OutboxEvent.tenant_id,
            OutboxEvent.topic,
            OutboxEvent.key,
            OutboxEvent.payload,
            OutboxEvent.headers,
            OutboxEvent.attempts,
        )
        .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= func.now())
        .order_by(OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return [
        PendingOutboxEvent(
            id=r.id,
            tenant_id=r.tenant_id,
            topic=r.topic,
            key=r.key,
            payload=r.payload,
            headers=r.headers,
            attempts=r.attempts,
        )
        for r in (await session.execute(stmt)).all()
    ]


@cross_tenant
async def mark_published(session: AsyncSession, ids: Sequence[uuid.UUID]) -> int:
    if not ids:
        return 0
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(list(ids)), OutboxEvent.published_at.is_(None))
        .values(published_at=func.now(), attempts=OutboxEvent.attempts + 1, last_error=None)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0))


@cross_tenant
async def mark_failed(
    session: AsyncSession,
    row_id: uuid.UUID,
    error: str,
    *,
    retry_delay: timedelta = timedelta(0),
) -> None:
    """Record a failed publish; the row is not claimed again before ``retry_delay`` passes."""
    if retry_delay < timedelta(0):
        raise ValueError("retry_delay must not be negative")
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id == row_id)
        .values(
            attempts=OutboxEvent.attempts + 1,
            last_error=error[:MAX_ERROR_LEN],
            # clock_timestamp(), not now(): now() is the start of this transaction, which
            # stayed open while the relay waited for broker acks, and would eat the delay.
            available_at=func.clock_timestamp() + retry_delay,
        )
    )
    await session.execute(stmt)
