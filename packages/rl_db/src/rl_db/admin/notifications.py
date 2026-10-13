"""Notification dispatch queue (cross-tenant poll, like ``analysis_jobs``; ADR-0011).

The dispatcher claims due rows in one short transaction (``FOR UPDATE SKIP LOCKED``), so
several worker processes never send the same notification concurrently, then delivers each
row in that row's own TenantContext. A crashed dispatcher's rows are taken back once their
lease expires. Delivery is therefore at least once: a crash after Slack accepted the
message but before the row was marked ``sent`` sends it again after the lease.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.tenancy import Principal, TenantContext
from rl_db.models.alerts import NotificationLog, NotificationStatus
from rl_db.repositories._tenancy import cross_tenant

MAX_ERROR_LEN = 2000


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    id: uuid.UUID
    tenant_id: uuid.UUID
    alert_rule_id: uuid.UUID | None
    kind: str
    subject_id: uuid.UUID
    channel: str
    payload: dict[str, Any]
    attempts: int  # including this one
    max_attempts: int

    def tenant_context(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, principal=Principal.SYSTEM)


@cross_tenant
async def claim_due_notifications(
    session: AsyncSession, *, worker_id: str, limit: int, lease: timedelta
) -> list[ClaimedNotification]:
    """Claim up to ``limit`` due rows (pending/failed past ``next_attempt_at``, or sending
    under an expired lease) and mark them ``sending``. The caller commits."""
    if limit < 1:
        return []
    due = (
        select(NotificationLog.id)
        .where(
            NotificationLog.attempts < NotificationLog.max_attempts,
            or_(
                and_(
                    NotificationLog.status.in_(
                        [NotificationStatus.PENDING, NotificationStatus.FAILED]
                    ),
                    NotificationLog.next_attempt_at <= func.now(),
                ),
                and_(
                    NotificationLog.status == NotificationStatus.SENDING,
                    NotificationLog.locked_at < func.now() - lease,
                ),
            ),
        )
        .order_by(NotificationLog.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(NotificationLog)
        .where(NotificationLog.id.in_(due.scalar_subquery()))
        .values(
            status=NotificationStatus.SENDING,
            attempts=NotificationLog.attempts + 1,
            locked_by=worker_id,
            locked_at=func.now(),
            updated_at=func.now(),
        )
        .returning(
            NotificationLog.id,
            NotificationLog.tenant_id,
            NotificationLog.alert_rule_id,
            NotificationLog.kind,
            NotificationLog.subject_id,
            NotificationLog.channel,
            NotificationLog.payload,
            NotificationLog.attempts,
            NotificationLog.max_attempts,
        )
    )
    rows = (await session.execute(stmt)).all()
    claimed = [
        ClaimedNotification(
            id=r.id,
            tenant_id=r.tenant_id,
            alert_rule_id=r.alert_rule_id,
            kind=r.kind,
            subject_id=r.subject_id,
            channel=r.channel,
            payload=dict(r.payload),
            attempts=r.attempts,
            max_attempts=r.max_attempts,
        )
        for r in rows
    ]
    return sorted(claimed, key=lambda n: n.id)


def _owned(row_id: uuid.UUID, worker_id: str) -> Any:
    return and_(
        NotificationLog.id == row_id,
        NotificationLog.status == NotificationStatus.SENDING,
        NotificationLog.locked_by == worker_id,
    )


@cross_tenant
async def mark_sent(session: AsyncSession, row_id: uuid.UUID, worker_id: str) -> bool:
    result = await session.execute(
        update(NotificationLog)
        .where(_owned(row_id, worker_id))
        .values(
            status=NotificationStatus.SENT,
            sent_at=func.now(),
            last_error=None,
            locked_by=None,
            locked_at=None,
            updated_at=func.now(),
        )
    )
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def mark_failed(
    session: AsyncSession,
    row_id: uuid.UUID,
    worker_id: str,
    *,
    error: str,
    retry_delay: timedelta,
    permanent: bool,
) -> str | None:
    """Record a failed attempt: ``dead`` if ``permanent`` or attempts are used up, else
    ``failed`` with the next attempt after ``retry_delay``. Returns the new status, or None
    if the claim was lost."""
    exhausted = or_(literal(permanent), NotificationLog.attempts >= NotificationLog.max_attempts)
    stmt = (
        update(NotificationLog)
        .where(_owned(row_id, worker_id))
        .values(
            status=case(
                (exhausted, literal(NotificationStatus.DEAD)),
                else_=literal(NotificationStatus.FAILED),
            ),
            next_attempt_at=case(
                (exhausted, NotificationLog.next_attempt_at),
                else_=func.now() + retry_delay,
            ),
            last_error=error[:MAX_ERROR_LEN],
            locked_by=None,
            locked_at=None,
            updated_at=func.now(),
        )
        .returning(NotificationLog.status)
    )
    status = (await session.execute(stmt)).scalar_one_or_none()
    return str(status) if status is not None else None


@cross_tenant
async def mark_skipped(
    session: AsyncSession, row_id: uuid.UUID, worker_id: str, *, reason: str
) -> bool:
    result = await session.execute(
        update(NotificationLog)
        .where(_owned(row_id, worker_id))
        .values(
            status=NotificationStatus.SKIPPED,
            last_error=reason[:MAX_ERROR_LEN],
            locked_by=None,
            locked_at=None,
            updated_at=func.now(),
        )
    )
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def reap_expired_leases(session: AsyncSession, *, lease: timedelta) -> int:
    """Mark ``dead`` the rows whose last allowed attempt crashed its dispatcher (claiming
    only re-takes expired leases while attempts remain)."""
    result = await session.execute(
        update(NotificationLog)
        .where(
            NotificationLog.status == NotificationStatus.SENDING,
            NotificationLog.locked_at < func.now() - lease,
            NotificationLog.attempts >= NotificationLog.max_attempts,
        )
        .values(
            status=NotificationStatus.DEAD,
            last_error="lease expired on the final attempt (dispatcher crashed or timed out)",
            locked_by=None,
            locked_at=None,
            updated_at=func.now(),
        )
    )
    return int(getattr(result, "rowcount", 0))


@cross_tenant
async def release_notification(session: AsyncSession, row_id: uuid.UUID, worker_id: str) -> bool:
    """Hand a claimed row back without using up an attempt (graceful shutdown)."""
    result = await session.execute(
        update(NotificationLog)
        .where(_owned(row_id, worker_id))
        .values(
            status=NotificationStatus.PENDING,
            attempts=NotificationLog.attempts - 1,
            locked_by=None,
            locked_at=None,
            updated_at=func.now(),
        )
    )
    return bool(getattr(result, "rowcount", 0))
