"""Read APIs over analyses and telemetry aggregates (scope ``read``, tenant-scoped).

* ``GET /api/v1/deployments/{id}/analysis`` — every analysis of a deployment and the latest one
  (the most complete kind: ``final`` > ``early_10m`` > ``early_5m``).
* ``GET /api/v1/regressions`` — deployments whose latest analysis is REGRESSED or WARNING.
* ``GET /api/v1/services`` — the service catalog with request rate, error rate and an
  **approximate** p95 over the last 15 complete minutes (from minute rollups).
* ``GET /api/v1/service-map`` — nodes and edges (cumulative call counts) of one environment, with
  per-node health from that environment's latest analysis.

Another tenant's ids answer exactly like missing ones (404, ADR-0010). Wording rule: the API says
"probable origin", never "root cause".
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from rl_api.auth import require_scope
from rl_api.deps import get_session
from rl_api.errors import ApiError
from rl_core.telemetry.rollup import approx_quantile
from rl_core.tenancy import TenantContext
from rl_db.models import RegressionAnalysisRecord
from rl_db.repositories import (
    RegressionCursor,
    get_deployment,
    latest_environment_analysis,
    list_analyses,
    list_regressions,
)
from rl_db.repositories.catalog import WindowHealth, service_catalog, service_map_edges

router = APIRouter(tags=["analysis"])

HEALTH_WINDOW = timedelta(minutes=15)

AnalysisKind = Literal["early_5m", "early_10m", "final"]
AnalysisStatus = Literal["HEALTHY", "WARNING", "REGRESSED", "INSUFFICIENT_DATA"]
SeverityBand = Literal["HEALTHY", "WARNING", "HIGH", "CRITICAL"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "Malformed cursor"},
    401: {"description": "Missing or invalid API key"},
    403: {"description": "The API key lacks the read scope"},
}


# ----------------------------------------------------------------------------- models
class AnalysisOut(BaseModel):
    id: uuid.UUID
    deployment_id: uuid.UUID
    kind: AnalysisKind
    status: AnalysisStatus
    severity_score: float | None = Field(description="Severity v1 (0-100), not a probability")
    severity_band: SeverityBand | None
    probable_origin: str | None = Field(
        description="Service the evidence points to; null when confidence is LOW"
    )
    confidence: Confidence | None
    engine_version: str
    windows: dict[str, Any]
    sampling: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] = Field(
        description="Full RegressionAnalysis JSON of engine_version (evidence per service, "
        "graph, triage, blast radius)"
    )

    @classmethod
    def of(cls, row: RegressionAnalysisRecord) -> AnalysisOut:
        return cls(
            id=row.id,
            deployment_id=row.deployment_id,
            kind=row.kind.value,
            status=row.status,
            severity_score=row.severity_score,
            severity_band=row.severity_band,
            probable_origin=row.probable_origin,
            confidence=row.confidence,
            engine_version=row.engine_version,
            windows=row.windows,
            sampling=row.sampling,
            created_at=row.created_at,
            updated_at=row.updated_at,
            result=row.result,
        )


class DeploymentAnalysis(BaseModel):
    deployment_id: uuid.UUID
    latest: AnalysisOut | None
    analyses: list[AnalysisOut]


class RegressionItem(BaseModel):
    deployment_id: uuid.UUID
    service: str
    environment: str
    version: str
    previous_version: str | None
    deployed_at: datetime
    analysis_id: uuid.UUID
    analysis_kind: AnalysisKind
    status: AnalysisStatus
    severity_score: float | None
    severity_band: SeverityBand | None
    probable_origin: str | None
    confidence: Confidence | None
    affected_services: list[str]
    analysed_at: datetime


class RegressionPage(BaseModel):
    items: list[RegressionItem]
    next_cursor: str | None


class LastDeployment(BaseModel):
    id: uuid.UUID
    version: str
    deployed_at: datetime


class ServiceHealth(BaseModel):
    window_start: datetime
    window_end: datetime
    requests: int
    request_rate_per_min: float
    errors: int
    error_rate: float | None
    latency_mean_ms: float | None
    latency_max_ms: float | None
    latency_p95_ms_approx: float | None = Field(
        description="Approximate p95 from a log-scale histogram; the exact value lies in "
        "latency_p95_bucket_ms"
    )
    latency_p95_bucket_ms: tuple[float, float | None] | None = Field(
        description="[low, high] bounds of the histogram bucket holding the p95 (high is null "
        "for the overflow bucket)"
    )
    latency_approximate: Literal[True] = True


class ServiceItem(BaseModel):
    service: str
    environment: str | None = Field(description="null when the service reported none")
    latest_version_seen: str | None
    latest_version_seen_at: datetime | None
    last_deployment: LastDeployment | None
    health: ServiceHealth


class ServicePage(BaseModel):
    items: list[ServiceItem]
    next_cursor: str | None


class ServiceMapNode(BaseModel):
    service: str
    status: AnalysisStatus | None = Field(description="From the latest analysis, if it covered it")
    impact: str | None = Field(description="Blast-radius class from the latest analysis")
    probable_origin: bool


class ServiceMapEdge(BaseModel):
    source: str
    target: str
    call_count: int = Field(description="Cumulative since first_seen")
    first_seen: datetime
    last_seen: datetime


class MapAnalysisRef(BaseModel):
    analysis_id: uuid.UUID
    deployment_id: uuid.UUID
    kind: AnalysisKind
    status: AnalysisStatus
    deployed_service: str
    analysed_at: datetime


class ServiceMap(BaseModel):
    environment: str
    since: datetime
    nodes: list[ServiceMapNode]
    edges: list[ServiceMapEdge]
    analysis: MapAnalysisRef | None


# ----------------------------------------------------------------------------- cursors
def _encode_cursor(data: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Untrusted input: any shape other than what _encode_cursor writes is a 400."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(data, dict) or set(data) != set(keys):
            raise TypeError("unexpected cursor shape")
        if not all(isinstance(data[k], str) for k in keys):
            raise TypeError("cursor fields must be strings")
        return {k: data[k] for k in keys}
    except (binascii.Error, ValueError, TypeError, RecursionError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor is malformed") from exc


def _regression_cursor(cursor: str) -> RegressionCursor:
    data = _decode_cursor(cursor, ("t", "id"))
    try:
        at = datetime.fromisoformat(data["t"])
        if at.tzinfo is None:
            raise ValueError("naive timestamp")
        return RegressionCursor(deployed_at=at.astimezone(UTC), deployment_id=uuid.UUID(data["id"]))
    except (ValueError, OverflowError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor is malformed") from exc


# ----------------------------------------------------------------------------- routes
@router.get(
    "/api/v1/deployments/{deployment_id}/analysis",
    response_model=DeploymentAnalysis,
    summary="Regression analyses of one deployment",
    responses={**_ERRORS, 404: {"description": "No such deployment for this tenant"}},
)
async def get_deployment_analysis(
    deployment_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(require_scope("read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeploymentAnalysis:
    if await get_deployment(session, ctx, deployment_id) is None:
        raise ApiError(404, "not_found", "deployment not found")
    analyses = [AnalysisOut.of(r) for r in await list_analyses(session, ctx, deployment_id)]
    return DeploymentAnalysis(
        deployment_id=deployment_id, latest=analyses[0] if analyses else None, analyses=analyses
    )


@router.get(
    "/api/v1/regressions",
    response_model=RegressionPage,
    summary="Deployments whose latest analysis is REGRESSED or WARNING, newest first",
    responses=_ERRORS,
)
async def list_regressions_route(
    ctx: Annotated[TenantContext, Depends(require_scope("read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    environment: Annotated[str | None, Query(max_length=64)] = None,
    service: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> RegressionPage:
    after = _regression_cursor(cursor) if cursor else None
    rows = await list_regressions(
        session, ctx, limit=limit + 1, environment=environment, service=service, after=after
    )
    page, more = rows[:limit], len(rows) > limit
    items = [
        RegressionItem(
            deployment_id=r.d_id,
            service=r.d_service,
            environment=r.d_environment,
            version=r.d_version,
            previous_version=r.d_previous_version,
            deployed_at=r.d_deployed_at,
            analysis_id=r.a_id,
            analysis_kind=r.a_kind.value,
            status=r.a_status,
            severity_score=r.a_severity_score,
            severity_band=r.a_severity_band,
            probable_origin=r.a_probable_origin,
            confidence=r.a_confidence,
            affected_services=[s for s in (r.a_affected or []) if isinstance(s, str)],
            analysed_at=r.a_updated_at,
        )
        for r in page
    ]
    next_cursor = (
        _encode_cursor({"t": page[-1].d_deployed_at.isoformat(), "id": str(page[-1].d_id)})
        if more
        else None
    )
    return RegressionPage(items=items, next_cursor=next_cursor)


def _health(h: WindowHealth, start: datetime, end: datetime) -> ServiceHealth:
    minutes = (end - start).total_seconds() / 60
    p95 = approx_quantile(h.buckets, 0.95) if h.buckets is not None else None
    return ServiceHealth(
        window_start=start,
        window_end=end,
        requests=h.requests,
        request_rate_per_min=h.requests / minutes,
        errors=h.errors,
        error_rate=h.errors / h.requests if h.requests else None,
        latency_mean_ms=h.duration_sum_ms / h.requests if h.requests else None,
        latency_max_ms=h.duration_max_ms if h.requests else None,
        latency_p95_ms_approx=None if p95 is None else p95.value_ms,
        latency_p95_bucket_ms=(
            None
            if p95 is None
            else (p95.low_ms, None if p95.high_ms == float("inf") else p95.high_ms)
        ),
    )


@router.get(
    "/api/v1/services",
    response_model=ServicePage,
    summary="Service catalog with request rate, error rate and approximate p95 (last 15 min)",
    responses=_ERRORS,
)
async def list_services(
    ctx: Annotated[TenantContext, Depends(require_scope("read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    environment: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> ServicePage:
    after = None
    if cursor:
        data = _decode_cursor(cursor, ("e", "s"))
        after = (data["e"], data["s"])
    now = datetime.now(UTC)
    entries = await service_catalog(
        session,
        ctx,
        now=now,
        window=HEALTH_WINDOW,
        limit=limit + 1,
        environment=environment,
        after=after,
    )
    page, more = entries[:limit], len(entries) > limit
    end = now.replace(second=0, microsecond=0)
    start = end - HEALTH_WINDOW
    items = [
        ServiceItem(
            service=e.service,
            environment=e.environment or None,
            latest_version_seen=e.version_seen,
            latest_version_seen_at=e.version_seen_at,
            last_deployment=(
                None
                if e.deployment_id is None or e.deployed_version is None or e.deployed_at is None
                else LastDeployment(
                    id=e.deployment_id, version=e.deployed_version, deployed_at=e.deployed_at
                )
            ),
            health=_health(e.health, start, end),
        )
        for e in page
    ]
    next_cursor = (
        _encode_cursor({"e": page[-1].environment, "s": page[-1].service}) if more else None
    )
    return ServicePage(items=items, next_cursor=next_cursor)


@router.get(
    "/api/v1/service-map",
    response_model=ServiceMap,
    summary="Service dependency map of one environment, with health from the latest analysis",
    responses=_ERRORS,
)
async def get_service_map(
    ctx: Annotated[TenantContext, Depends(require_scope("read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    environment: Annotated[str, Query(min_length=1, max_length=64)],
    window_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
) -> ServiceMap:
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    edges, active = await service_map_edges(session, ctx, environment=environment, since=since)
    latest = await latest_environment_analysis(session, ctx, environment)
    statuses: dict[str, str] = {}
    impacts: dict[str, str] = {}
    origin: str | None = None
    ref: MapAnalysisRef | None = None
    if latest is not None:
        result = latest.result
        statuses = {
            s["service"]: s["status"]
            for s in result.get("services", [])
            if isinstance(s, dict) and "service" in s and "status" in s
        }
        radius = result.get("blast_radius") or {}
        impacts = {
            n["service"]: n["impact"]
            for n in radius.get("nodes", [])
            if isinstance(n, dict) and "service" in n and "impact" in n
        }
        origin = latest.probable_origin
        if origin is not None:
            impacts.setdefault(origin, "PROBABLE_ORIGIN")
        ref = MapAnalysisRef(
            analysis_id=latest.id,
            deployment_id=latest.deployment_id,
            kind=latest.kind.value,
            status=latest.status,
            deployed_service=str(result.get("deployment", {}).get("service", "")),
            analysed_at=latest.updated_at,
        )
    names = sorted({e.caller for e in edges} | {e.callee for e in edges} | active)
    return ServiceMap(
        environment=environment,
        since=since,
        nodes=[
            ServiceMapNode(
                service=name,
                status=statuses.get(name),
                impact=impacts.get(name),
                probable_origin=name == origin,
            )
            for name in names
        ],
        edges=[
            ServiceMapEdge(
                source=e.caller,
                target=e.callee,
                call_count=e.call_count,
                first_seen=e.first_seen,
                last_seen=e.last_seen,
            )
            for e in edges
        ],
        analysis=ref,
    )
