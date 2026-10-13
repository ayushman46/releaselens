"""Plan retention and housekeeping (cross-tenant maintenance, run by the worker).

Retention is a plan limit (``rl_core.plans``): Free keeps 7 days, Team 30, Enterprise 90 by
default. Demo organizations are exempt so the scripted demo data never disappears.
Deletes run in bounded batches so one purge never holds long locks.

Span partitions (M7) are dropped by the telemetry retention job, which reads the same
``retention_days``; this module covers the OLTP tables.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.plans import PLANS, limits_for
from rl_db.models.accounts import AuthSession, OAuthState, UserToken
from rl_db.models.deployments import Deployment
from rl_db.models.identity import Organization
from rl_db.repositories._tenancy import cross_tenant


def _retention_days_sql() -> object:
    """``CASE organizations.plan WHEN 'free' THEN 7 …`` built from the plan table."""
    return case(
        *((Organization.plan == plan, limits.retention_days) for plan, limits in PLANS.items()),
        else_=limits_for("free").retention_days,
    )


@cross_tenant
async def purge_expired_deployments(session: AsyncSession, *, batch_size: int = 1000) -> int:
    """Delete one batch of deployments older than their organization's plan retention
    (their analysis jobs cascade). Returns the number deleted; call until it returns 0."""
    expired = (
        select(Deployment.id)
        .join(Organization, Organization.id == Deployment.tenant_id)
        .where(
            Organization.is_demo.is_(False),
            Deployment.deployed_at
            < func.now() - func.make_interval(0, 0, 0, _retention_days_sql()),
        )
        .limit(batch_size)
    )
    result = await session.execute(
        delete(Deployment).where(Deployment.id.in_(expired.scalar_subquery()))
    )
    return int(getattr(result, "rowcount", 0))


@cross_tenant
async def purge_stale_auth_rows(session: AsyncSession, *, grace: timedelta) -> int:
    """Drop sessions, one-time tokens and OAuth states that ended more than ``grace`` ago."""
    total = 0
    for stmt in (
        delete(AuthSession).where(AuthSession.expires_at < func.now() - grace),
        delete(AuthSession).where(AuthSession.revoked_at < func.now() - grace),
        delete(UserToken).where(UserToken.expires_at < func.now() - grace),
        delete(OAuthState).where(OAuthState.expires_at < func.now()),
    ):
        result = await session.execute(stmt)
        total += int(getattr(result, "rowcount", 0))
    return total
