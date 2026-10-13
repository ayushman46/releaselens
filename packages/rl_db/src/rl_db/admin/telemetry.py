"""Cross-tenant telemetry helpers for the span consumer and the maintenance loop.

* :func:`existing_tenants`: a span batch names its tenant in the envelope. ``spans`` has no
  foreign key to ``organizations`` (a per-row check on the hottest insert path), so the consumer
  checks the batch's tenants once and dead-letters envelopes of tenants that do not exist.
* :func:`delete_rollups_older_than`: minute-rollup retention (spans use partition drops).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from rl_db.models.identity import Organization
from rl_db.models.telemetry import ServiceMinuteRollup
from rl_db.repositories._tenancy import cross_tenant


@cross_tenant
async def existing_tenants(
    session: AsyncSession, tenant_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    if not tenant_ids:
        return set()
    rows = await session.scalars(
        select(Organization.id).where(Organization.id.in_(list(set(tenant_ids))))
    )
    return set(rows)


@cross_tenant
async def delete_rollups_older_than(
    session: AsyncSession, days: int, *, now: datetime | None = None
) -> int:
    if days < 1:
        raise ValueError("retention must be at least one day")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    result = await session.execute(
        delete(ServiceMinuteRollup).where(ServiceMinuteRollup.minute < cutoff)
    )
    return int(getattr(result, "rowcount", 0))
