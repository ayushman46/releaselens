"""Subscription reads and usage counters (tenant-scoped, ADR-0010)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.tenancy import TenantContext
from rl_db.models.billing import Subscription, UsageCounter
from rl_db.repositories._tenancy import tenant_scoped


def month_start(day: date) -> date:
    return day.replace(day=1)


@tenant_scoped
async def get_subscription(session: AsyncSession, ctx: TenantContext) -> Subscription | None:
    stmt = select(Subscription).where(Subscription.tenant_id == ctx.tenant_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@tenant_scoped
async def increment_usage(
    session: AsyncSession, ctx: TenantContext, metric: str, *, period_start: date, by: int = 1
) -> int:
    """Add ``by`` to this period's counter with one UPSERT; returns the new value."""
    stmt = (
        pg_insert(UsageCounter)
        .values(tenant_id=ctx.tenant_id, metric=metric, period_start=period_start, value=by)
        .on_conflict_do_update(
            index_elements=["tenant_id", "metric", "period_start"],
            set_={"value": UsageCounter.value + by, "updated_at": func.now()},
        )
        .returning(UsageCounter.value)
    )
    return int((await session.execute(stmt)).scalar_one())


@tenant_scoped
async def get_usage(
    session: AsyncSession, ctx: TenantContext, *, period_start: date
) -> dict[str, int]:
    stmt = select(UsageCounter.metric, UsageCounter.value).where(
        UsageCounter.tenant_id == ctx.tenant_id, UsageCounter.period_start == period_start
    )
    return {r.metric: int(r.value) for r in (await session.execute(stmt)).all()}
