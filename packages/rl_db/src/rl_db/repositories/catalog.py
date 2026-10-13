"""Service catalog and service map reads (tenant-scoped), built from the telemetry aggregates.

* A service is **known** in an environment when it has minute rollups in the last
  :data:`CATALOG_LOOKBACK`, a recorded deployment, or a service edge.
* Health numbers come from ``service_minute_rollups`` over a recent window of whole minutes; the
  p95 is approximated from the merged latency histogram (see :mod:`rl_core.telemetry.rollup`).
* Edge call counts are cumulative since the edge was first seen; ``since`` filters edges by when
  they were *last* seen.
* Environments (:func:`list_environments`) are the distinct names in deployments, minute rollups
  and edges; ``''`` ("not reported") is left out.

The rollup primary key is ``(tenant_id, environment, service, minute)``. Postgres 16 has no skip
scan, so the distinct ``(environment, service)`` pairs of a tenant are walked with a recursive
"loose index scan" (one index probe per pair) instead of reading every minute row.
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


MAX_ENVIRONMENTS: Final = 500

# The tenant's distinct (environment, service) rollup pairs: one index probe each.
_ENVIRONMENTS: Final = text(
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
), seen AS (
    SELECT p.environment, m.minute AS at, 'telemetry' AS source
      FROM pairs p CROSS JOIN LATERAL (
          SELECT minute FROM service_minute_rollups
           WHERE tenant_id = :tenant AND environment = p.environment AND service = p.service
           ORDER BY minute DESC LIMIT 1
      ) m
    UNION ALL
    SELECT environment, max(last_seen), 'telemetry' FROM service_edges
     WHERE tenant_id = :tenant GROUP BY environment
    UNION ALL
    SELECT environment, max(deployed_at), 'deployment' FROM deployments
     WHERE tenant_id = :tenant GROUP BY environment
)
SELECT environment,
       max(at) AS last_seen,
       max(at) FILTER (WHERE source = 'deployment') AS last_deployment_at,
       max(at) FILTER (WHERE source = 'telemetry') AS last_telemetry_at
FROM seen
WHERE environment <> ''
GROUP BY environment
ORDER BY environment
LIMIT :limit
"""
)

MAX_SERIES_SERVICES: Final = 500

# Every service of one environment (loose index scan), then each one's minutes in the range.
_SERIES_ALL: Final = text(
    """
WITH RECURSIVE svc AS (
    (SELECT service FROM service_minute_rollups
      WHERE tenant_id = :tenant AND environment = :environment ORDER BY service LIMIT 1)
    UNION ALL
    SELECT n.service FROM svc s CROSS JOIN LATERAL (
        SELECT service FROM service_minute_rollups
         WHERE tenant_id = :tenant AND environment = :environment AND service > s.service
         ORDER BY service LIMIT 1
    ) n
)
SELECT r.service, r.minute, r.request_count, r.error_count, r.duration_sum_ms, r.duration_max_ms,
       r.latency_buckets
FROM (SELECT service FROM svc LIMIT :max_services) s
JOIN service_minute_rollups r
  ON r.tenant_id = :tenant AND r.environment = :environment AND r.service = s.service
 AND r.minute >= :start AND r.minute < :end
ORDER BY r.service, r.minute
"""
)

_SERIES_ONE: Final = text(
    """
SELECT service, minute, request_count, error_count, duration_sum_ms, duration_max_ms,
       latency_buckets
FROM service_minute_rollups
WHERE tenant_id = :tenant AND environment = :environment AND service = :service
  AND minute >= :start AND minute < :end
ORDER BY minute
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


@dataclass(frozen=True, slots=True)
class EnvironmentSeen:
    name: str
    last_seen: datetime
    last_deployment_at: datetime | None
    last_telemetry_at: datetime | None  # newest rollup minute (its start) or edge call


async def list_environments(session: AsyncSession, ctx: TenantContext) -> list[EnvironmentSeen]:
    """The tenant's environments seen in deployments, minute rollups or service edges, by name
    (at most :data:`MAX_ENVIRONMENTS`)."""
    rows = await session.execute(
        _ENVIRONMENTS, {"tenant": ctx.tenant_id, "limit": MAX_ENVIRONMENTS}
    )
    return [
        EnvironmentSeen(
            name=r.environment,
            last_seen=r.last_seen,
            last_deployment_at=r.last_deployment_at,
            last_telemetry_at=r.last_telemetry_at,
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class MinuteRow:
    service: str
    minute: datetime
    requests: int
    errors: int
    duration_sum_ms: float
    duration_max_ms: float
    buckets: list[int]


async def minute_series(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    environment: str,
    start: datetime,
    end: datetime,
    service: str | None = None,
) -> list[MinuteRow]:
    """Minute rollups of ``environment`` with ``start <= minute < end``, ordered by service and
    minute: one service, or every service of the environment (at most
    :data:`MAX_SERIES_SERVICES`, by name). Minutes without requests have no row."""
    params: dict[str, Any] = {
        "tenant": ctx.tenant_id,
        "environment": environment,
        "start": start,
        "end": end,
    }
    if service is None:
        result = await session.execute(_SERIES_ALL, {**params, "max_services": MAX_SERIES_SERVICES})
    else:
        result = await session.execute(_SERIES_ONE, {**params, "service": service})
    return [
        MinuteRow(
            service=r.service,
            minute=r.minute,
            requests=int(r.request_count),
            errors=int(r.error_count),
            duration_sum_ms=float(r.duration_sum_ms),
            duration_max_ms=float(r.duration_max_ms),
            buckets=list(r.latency_buckets),
        )
        for r in result
    ]
