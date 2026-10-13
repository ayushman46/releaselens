"""Exact tenant-level counts for the overview page (tenant-scoped, ADR-0010).

* **deployments**: deployments with ``deployed_at >= deployments_since``.
* **regressions**: of those, the ones whose latest analysis (final > early_10m > early_5m) is
  REGRESSED or WARNING — the total of ``GET /api/v1/regressions?since=``.
* **active regressions**: ``(service, environment)`` pairs whose *newest* deployment's latest
  analysis is REGRESSED or WARNING. A later deployment of the same service supersedes it.
* **risk assessments by band**: assessments created at or after ``assessments_since``.
* **services seen**: distinct service names with requests (minute rollups, i.e. SERVER spans) in
  a minute starting at or after ``services_since``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.tenancy import TenantContext
from rl_db.repositories._tenancy import tenant_scoped
from rl_db.repositories.analyses import REGRESSION_STATUSES

_DEPLOYMENTS: Final = text(
    """
SELECT count(*) AS deployments,
       count(*) FILTER (WHERE a.status = ANY(CAST(:statuses AS text[]))) AS regressions
FROM deployments d
LEFT JOIN LATERAL (
    SELECT status FROM regression_analyses ra
     WHERE ra.tenant_id = d.tenant_id AND ra.deployment_id = d.id
     ORDER BY ra.kind DESC LIMIT 1
) a ON true
WHERE d.tenant_id = :tenant AND d.deployed_at >= :since
"""
)

_ACTIVE_REGRESSIONS: Final = text(
    """
SELECT count(*) FROM (
    SELECT DISTINCT ON (service, environment) id, tenant_id FROM deployments
     WHERE tenant_id = :tenant
     ORDER BY service, environment, deployed_at DESC, id DESC
) d
CROSS JOIN LATERAL (
    SELECT status FROM regression_analyses ra
     WHERE ra.tenant_id = d.tenant_id AND ra.deployment_id = d.id
     ORDER BY ra.kind DESC LIMIT 1
) a
WHERE a.status = ANY(CAST(:statuses AS text[]))
"""
)

_RISK_BANDS: Final = text(
    """
SELECT risk_band, count(*) AS n FROM risk_assessments
WHERE tenant_id = :tenant AND created_at >= :since
GROUP BY risk_band
"""
)

# Loose index scan over the (environment, service) rollup pairs (see rl_db.repositories.catalog).
_SERVICES_SEEN: Final = text(
    """
WITH RECURSIVE pairs AS (
    (SELECT environment, service FROM service_minute_rollups
      WHERE tenant_id = :tenant ORDER BY environment, service LIMIT 1)
    UNION ALL
    SELECT n.environment, n.service FROM pairs p CROSS JOIN LATERAL (
        SELECT environment, service FROM service_minute_rollups
         WHERE tenant_id = :tenant AND (environment, service) > (p.environment, p.service)
         ORDER BY environment, service LIMIT 1
    ) n
)
SELECT count(DISTINCT p.service) FROM pairs p
WHERE EXISTS (
    SELECT 1 FROM service_minute_rollups r
     WHERE r.tenant_id = :tenant AND r.environment = p.environment AND r.service = p.service
       AND r.minute >= :since
)
"""
)


@dataclass(frozen=True, slots=True)
class OverviewCounts:
    deployments: int
    regressions: int
    active_regressions: int
    risk_assessments_by_band: dict[str, int]
    services_seen: int


@tenant_scoped
async def overview_counts(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    deployments_since: datetime,
    assessments_since: datetime,
    services_since: datetime,
) -> OverviewCounts:
    """Exact counts (module docstring)."""
    tenant = ctx.tenant_id
    statuses = list(REGRESSION_STATUSES)
    deploys = (
        await session.execute(
            _DEPLOYMENTS, {"tenant": tenant, "since": deployments_since, "statuses": statuses}
        )
    ).one()
    active = (
        await session.execute(_ACTIVE_REGRESSIONS, {"tenant": tenant, "statuses": statuses})
    ).scalar_one()
    bands = {
        r.risk_band: int(r.n)
        for r in await session.execute(_RISK_BANDS, {"tenant": tenant, "since": assessments_since})
    }
    services = (
        await session.execute(_SERVICES_SEEN, {"tenant": tenant, "since": services_since})
    ).scalar_one()
    return OverviewCounts(
        deployments=int(deploys.deployments),
        regressions=int(deploys.regressions),
        active_regressions=int(active),
        risk_assessments_by_band=bands,
        services_seen=int(services),
    )
