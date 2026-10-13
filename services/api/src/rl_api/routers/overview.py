"""Tenant-level overview reads (scope ``read`` or a session plus ``X-RL-Org``).

* ``GET /api/v1/environments`` — the environments seen in deployments, minute rollups and
  service edges, with when each was last seen.
* ``GET /api/v1/stats/overview`` — exact counts for the overview page (definitions in
  :mod:`rl_db.repositories.stats`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from rl_api.deps import get_session
from rl_api.principal import require_tenant_read
from rl_core.tenancy import TenantContext
from rl_db.models.risk import RISK_BANDS
from rl_db.repositories.catalog import list_environments
from rl_db.repositories.stats import overview_counts

router = APIRouter(prefix="/api/v1", tags=["overview"])

DEPLOYMENTS_WINDOW = timedelta(days=7)
ASSESSMENTS_WINDOW = timedelta(days=7)
SERVICES_WINDOW = timedelta(hours=24)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid credentials"},
    403: {"description": "The API key lacks the read scope"},
}


class SeenEnvironment(BaseModel):
    name: str
    last_seen: datetime = Field(description="Newest of last_deployment_at and last_telemetry_at")
    last_deployment_at: datetime | None = Field(description="Newest deployed_at in it")
    last_telemetry_at: datetime | None = Field(
        description="Newest request minute (its start) or service-to-service call in it"
    )


class SeenEnvironmentList(BaseModel):
    items: list[SeenEnvironment] = Field(description="By name (at most 500)")


class RiskBandCounts(BaseModel):
    LOW: int
    MEDIUM: int
    HIGH: int
    CRITICAL: int
    total: int


class OverviewStats(BaseModel):
    as_of: datetime
    deployments_since: datetime
    deployments_7d: int = Field(description="Deployments with deployed_at in the last 7 days")
    regressions_7d: int = Field(
        description="Of those, deployments whose latest analysis is REGRESSED or WARNING "
        "(= GET /api/v1/regressions?since=deployments_since)"
    )
    active_regressions: int = Field(
        description="(service, environment) pairs whose newest deployment's latest analysis is "
        "REGRESSED or WARNING; a later deployment of the service supersedes it"
    )
    risk_assessments_since: datetime
    risk_assessments_7d: RiskBandCounts = Field(
        description="Risk assessments created in the last 7 days, by band"
    )
    services_since: datetime
    services_seen_24h: int = Field(
        description="Distinct services with requests (SERVER spans) in the last 24 hours"
    )


@router.get(
    "/environments",
    response_model=SeenEnvironmentList,
    summary="Environments seen in deployments and telemetry, with when each was last seen",
    responses=_ERRORS,
)
async def list_environments_route(
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SeenEnvironmentList:
    rows = await list_environments(session, ctx)
    return SeenEnvironmentList(
        items=[
            SeenEnvironment(
                name=r.name,
                last_seen=r.last_seen,
                last_deployment_at=r.last_deployment_at,
                last_telemetry_at=r.last_telemetry_at,
            )
            for r in rows
        ]
    )


@router.get(
    "/stats/overview",
    response_model=OverviewStats,
    summary="Exact counts for the overview: deployments, regressions, risk, services",
    responses=_ERRORS,
)
async def stats_overview(
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OverviewStats:
    now = datetime.now(UTC)
    deployments_since = now - DEPLOYMENTS_WINDOW
    assessments_since = now - ASSESSMENTS_WINDOW
    services_since = now - SERVICES_WINDOW
    counts = await overview_counts(
        session,
        ctx,
        deployments_since=deployments_since,
        assessments_since=assessments_since,
        services_since=services_since,
    )
    bands = {band: counts.risk_assessments_by_band.get(band, 0) for band in RISK_BANDS}
    return OverviewStats(
        as_of=now,
        deployments_since=deployments_since,
        deployments_7d=counts.deployments,
        regressions_7d=counts.regressions,
        active_regressions=counts.active_regressions,
        risk_assessments_since=assessments_since,
        risk_assessments_7d=RiskBandCounts(**bands, total=sum(bands.values())),
        services_since=services_since,
        services_seen_24h=counts.services_seen,
    )
