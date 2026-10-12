"""Service catalog and service map reads (tenant-scoped), built from the telemetry aggregates.

* A service is **known** in an environment when it has minute rollups in the last
  :data:`CATALOG_LOOKBACK`, a recorded deployment, or a service edge.
* Health numbers come from ``service_minute_rollups`` over a recent window of whole minutes; the
  p95 is approximated from the merged latency histogram (see :mod:`rl_core.telemetry.rollup`).
* Edge call counts are cumulative since the edge was first seen; ``since`` filters edges by when
  they were *last* seen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.telemetry.rollup import merge_histograms
from rl_core.tenancy import TenantContext

CATALOG_LOOKBACK: Final = timedelta(days=7)
VERSION_LOOKBACK: Final = timedelta(days=1)
MAX_MAP_EDGES: Final = 5000

_CATALOG_PAGE: Final = text(
    """
WITH known AS (
    SELECT environment, service FROM service_minute_rollups
     WHERE tenant_id = :tenant AND minute >= :lookback
    UNION SELECT environment, service FROM deployments WHERE tenant_id = :tenant
    UNION SELECT environment, caller_service FROM service_edges WHERE tenant_id = :tenant
    UNION SELECT environment, callee_service FROM service_edges WHERE tenant_id = :tenant
), page AS (
    SELECT environment, service FROM known
     WHERE (CAST(:environment AS text) IS NULL OR environment = :environment)
       AND (environment, service) > (:after_environment, :after_service)
     ORDER BY environment, service
     LIMIT :limit
)
SELECT p.environment, p.service,
       d.id AS deployment_id, d.version AS deployed_version, d.deployed_at,
       v.service_version AS version_seen, v.start_time AS version_seen_at
FROM page p
LEFT JOIN LATERAL (
    SELECT id, version, deployed_at FROM deployments
     WHERE tenant_id = :tenant AND environment = p.environment AND service = p.service
     ORDER BY deployed_at DESC, id DESC LIMIT 1
) d ON true
LEFT JOIN LATERAL (
    SELECT service_version, start_time FROM spans
     WHERE tenant_id = :tenant AND environment = p.environment AND service = p.service
       AND start_time >= :version_lookback AND service_version IS NOT NULL
     ORDER BY start_time DESC LIMIT 1
) v ON true
ORDER BY p.environment, p.service
"""
)

_ROLLUPS: Final = text(
    """
SELECT environment, service, request_count, error_count, duration_sum_ms, duration_max_ms,
       latency_buckets
FROM service_minute_rollups
WHERE tenant_id = :tenant AND minute >= :start AND minute < :end
  AND (environment, service) IN (
      SELECT * FROM unnest(CAST(:environments AS text[]), CAST(:services AS text[]))
  )
"""
)

_EDGES: Final = text(
    """
SELECT caller_service, callee_service, call_count, first_seen, last_seen
FROM service_edges
WHERE tenant_id = :tenant AND environment = :environment AND last_seen >= :since
ORDER BY caller_service, callee_service
LIMIT :limit
"""
)

_ACTIVE_SERVICES: Final = text(
    """
SELECT DISTINCT service FROM service_minute_rollups
WHERE tenant_id = :tenant AND environment = :environment AND minute >= :since
"""
)


@dataclass
class WindowHealth:
    requests: int = 0
    errors: int = 0
    duration_sum_ms: float = 0.0
    duration_max_ms: float = 0.0
    buckets: list[int] | None = None

    def add(self, row: Any) -> None:
        self.requests += int(row.request_count)
        self.errors += int(row.error_count)
        self.duration_sum_ms += float(row.duration_sum_ms)
        self.duration_max_ms = max(self.duration_max_ms, float(row.duration_max_ms))
        buckets = list(row.latency_buckets)
        self.buckets = buckets if self.buckets is None else merge_histograms(self.buckets, buckets)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    environment: str
    service: str
    deployment_id: Any
    deployed_version: str | None
    deployed_at: datetime | None
    version_seen: str | None
    version_seen_at: datetime | None
    health: WindowHealth


async def service_catalog(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    now: datetime,
    window: timedelta,
    limit: int,
    environment: str | None = None,
    after: tuple[str, str] | None = None,
) -> list[CatalogEntry]:
    """One page of known services, ordered by ``(environment, service)``.

    ``window`` is measured in whole minutes ending at the start of ``now``'s minute, so the
    still-filling current minute never drags rates down.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be within 1..1000")
    end = now.replace(second=0, microsecond=0)
    start = end - window
    # ('', '') sorts before every row: '' is the smallest environment and services are non-empty.
    after_env, after_service = after if after is not None else ("", "")
    rows = (
        await session.execute(
            _CATALOG_PAGE,
            {
                "tenant": ctx.tenant_id,
                "lookback": now - CATALOG_LOOKBACK,
                "version_lookback": now - VERSION_LOOKBACK,
                "environment": environment,
                "after_environment": after_env,
                "after_service": after_service,
                "limit": limit,
            },
        )
    ).all()
    health: dict[tuple[str, str], WindowHealth] = {
        (r.environment, r.service): WindowHealth() for r in rows
    }
    if rows:
        for r in await session.execute(
            _ROLLUPS,
            {
                "tenant": ctx.tenant_id,
                "start": start,
                "end": end,
                "environments": [r.environment for r in rows],
                "services": [r.service for r in rows],
            },
        ):
            health[(r.environment, r.service)].add(r)
    return [
        CatalogEntry(
            environment=r.environment,
            service=r.service,
            deployment_id=r.deployment_id,
            deployed_version=r.deployed_version,
            deployed_at=r.deployed_at,
            version_seen=r.version_seen,
            version_seen_at=r.version_seen_at,
            health=health[(r.environment, r.service)],
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class MapEdge:
    caller: str
    callee: str
    call_count: int
    first_seen: datetime
    last_seen: datetime


async def service_map_edges(
    session: AsyncSession, ctx: TenantContext, *, environment: str, since: datetime
) -> tuple[list[MapEdge], set[str]]:
    """Edges last seen at or after ``since`` (at most :data:`MAX_MAP_EDGES`), and every service
    with requests since then (so services without edges still appear as nodes)."""
    params = {"tenant": ctx.tenant_id, "environment": environment, "since": since}
    edges = [
        MapEdge(r.caller_service, r.callee_service, int(r.call_count), r.first_seen, r.last_seen)
        for r in await session.execute(_EDGES, {**params, "limit": MAX_MAP_EDGES})
    ]
    active = {r.service for r in await session.execute(_ACTIVE_SERVICES, params)}
    return edges, active
