"""Regression analyses: the span window an analysis reads, and its result rows (tenant-scoped).

**Span loading.** :func:`load_analysis_spans` reads every span of the tenant whose start lies in
``[start, end + LOAD_TAIL_MARGIN]`` in the deployment's environment, plus spans that reported no
environment (the engine keeps those; see ``rl_core.regression.analysis``). The tail margin keeps
the children of requests that start just before the window end: without them, a late request's
self-time would include time it actually spent waiting on a callee.

When more than ``max_spans`` spans match, whole **traces** are sampled deterministically: a trace
is kept when the first 32 bits of ``md5(trace_id)`` fall below ``max_spans / matched · 2³²``.
Every span of a kept trace is kept, so self-time and the service graph stay consistent; counts
(and so throughput) shrink by the sample fraction, which the result records.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, func, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.telemetry.model import Span, SpanKind, StatusCode
from rl_core.tenancy import TenantContext
from rl_db.models.analyses import RegressionAnalysisRecord
from rl_db.models.deployments import Deployment
from rl_db.models.jobs import JobKind

LOAD_TAIL_MARGIN: Final = timedelta(minutes=2)
SAMPLE_METHOD: Final = "trace_id_md5_32"
_HASH32: Final = "('x' || substr(md5(trace_id), 1, 8))::bit(32)::bigint"

_SPAN_FILTER: Final = (
    "tenant_id = $1 AND (environment = $2 OR environment IS NULL) "
    "AND start_time >= $3 AND start_time <= $4"
)
_COUNT_SPANS: Final = f"SELECT count(*) FROM spans WHERE {_SPAN_FILTER}"  # noqa: S608
_LOAD_SPANS: Final = (
    "SELECT trace_id, span_id, parent_span_id, service, service_version, environment, name, "  # noqa: S608
    "kind, start_ns, end_ns, status_code, http_route, http_status "
    f"FROM spans WHERE {_SPAN_FILTER} {{sample}} ORDER BY start_ns, trace_id, span_id"
)


@dataclass(frozen=True, slots=True)
class SpanLoad:
    spans: list[Span]
    matched: int
    max_spans: int
    sample_threshold: int | None  # keep traces whose 32-bit hash is below this; None = all

    @property
    def sampled(self) -> bool:
        return self.sample_threshold is not None

    def describe(self, start: datetime, end: datetime) -> dict[str, Any]:
        """JSON for ``regression_analyses.sampling``."""
        return {
            "spans_matched": self.matched,
            "spans_loaded": len(self.spans),
            "max_spans": self.max_spans,
            "sampled": self.sampled,
            "method": SAMPLE_METHOD if self.sampled else None,
            "sample_fraction": (
                None if self.sample_threshold is None else self.sample_threshold / 2**32
            ),
            "loaded_from": start.isoformat(),
            "loaded_to": end.isoformat(),
        }


def sample_threshold(matched: int, max_spans: int) -> int | None:
    """Hash threshold that keeps about ``max_spans`` of ``matched`` spans (None: keep all)."""
    if max_spans < 1:
        raise ValueError("max_spans must be positive")
    if matched <= max_spans:
        return None
    return max(1, math.floor(max_spans / matched * 2**32))


async def load_analysis_spans(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    environment: str,
    start: datetime,
    end: datetime,
    max_spans: int,
) -> SpanLoad:
    """The spans an analysis of ``[start, end]`` needs (module docstring)."""
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    driver: Any = raw.driver_connection
    upper = end + LOAD_TAIL_MARGIN
    args = (ctx.tenant_id, environment, start, upper)
    matched = int(await driver.fetchval(_COUNT_SPANS, *args))
    threshold = sample_threshold(matched, max_spans)
    sample = "" if threshold is None else f"AND {_HASH32} < {int(threshold)}"
    rows = await driver.fetch(_LOAD_SPANS.format(sample=sample), *args)
    tenant = str(ctx.tenant_id)
    spans = [
        Span(
            tenant_id=tenant,
            trace_id=r["trace_id"],
            span_id=r["span_id"],
            parent_span_id=r["parent_span_id"],
            service=r["service"],
            service_version=r["service_version"],
            environment=r["environment"],
            name=r["name"],
            kind=SpanKind(r["kind"]),
            start_ns=r["start_ns"],
            end_ns=r["end_ns"],
            status_code=StatusCode(r["status_code"]),
            http_route=r["http_route"],
            http_status=r["http_status"],
        )
        for r in rows
    ]
    return SpanLoad(spans=spans, matched=matched, max_spans=max_spans, sample_threshold=threshold)


# ----------------------------------------------------------------------------- results
@dataclass(frozen=True, slots=True)
class AnalysisWrite:
    deployment_id: uuid.UUID
    kind: JobKind
    status: str
    severity_score: float | None
    severity_band: str | None
    probable_origin: str | None
    confidence: str | None
    result: dict[str, Any]
    engine_version: str
    windows: dict[str, Any]
    sampling: dict[str, Any]


async def upsert_analysis(
    session: AsyncSession, ctx: TenantContext, row: AnalysisWrite
) -> uuid.UUID:
    """Insert or replace the analysis of ``(deployment, kind)``; returns its (stable) id."""
    values = {
        "status": row.status,
        "severity_score": row.severity_score,
        "severity_band": row.severity_band,
        "probable_origin": row.probable_origin,
        "confidence": row.confidence,
        "result": row.result,
        "engine_version": row.engine_version,
        "windows": row.windows,
        "sampling": row.sampling,
    }
    stmt = (
        pg_insert(RegressionAnalysisRecord)
        .values(
            id=uuid7(),
            tenant_id=ctx.tenant_id,
            deployment_id=row.deployment_id,
            kind=row.kind,
            **values,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "deployment_id", "kind"],
            set_={**values, "updated_at": func.now()},
        )
        .returning(RegressionAnalysisRecord.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_analyses(
    session: AsyncSession, ctx: TenantContext, deployment_id: uuid.UUID
) -> list[RegressionAnalysisRecord]:
    """Every analysis of the deployment, most complete kind first (final, early_10m, early_5m)."""
    stmt = (
        select(RegressionAnalysisRecord)
        .where(
            RegressionAnalysisRecord.tenant_id == ctx.tenant_id,
            RegressionAnalysisRecord.deployment_id == deployment_id,
        )
        .order_by(RegressionAnalysisRecord.kind.desc())
    )
    return list((await session.execute(stmt)).scalars())


# ----------------------------------------------------------------------------- regressions
REGRESSION_STATUSES: Final = ("REGRESSED", "WARNING")


@dataclass(frozen=True, slots=True)
class RegressionCursor:
    deployed_at: datetime
    deployment_id: uuid.UUID


def _latest_per_deployment(ctx: TenantContext) -> Any:
    """The most complete analysis of every deployment of the tenant (final > early_10m > …)."""
    ra = RegressionAnalysisRecord
    return (
        select(ra)
        .where(ra.tenant_id == ctx.tenant_id)
        .order_by(ra.deployment_id, ra.kind.desc())
        .ext(distinct_on(ra.deployment_id))
        .subquery("latest")
    )


async def list_regressions(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int,
    environment: str | None = None,
    service: str | None = None,
    after: RegressionCursor | None = None,
) -> Sequence[Row[Any]]:
    """Deployments whose latest analysis is REGRESSED or WARNING, newest deployment first.

    Each row has the deployment's columns (``d_*``) and the analysis's (``a_*``).
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be within 1..1000")
    latest = _latest_per_deployment(ctx)
    d = Deployment
    stmt = (
        select(
            d.id.label("d_id"),
            d.service.label("d_service"),
            d.environment.label("d_environment"),
            d.version.label("d_version"),
            d.previous_version.label("d_previous_version"),
            d.deployed_at.label("d_deployed_at"),
            latest.c.id.label("a_id"),
            latest.c.kind.label("a_kind"),
            latest.c.status.label("a_status"),
            latest.c.severity_score.label("a_severity_score"),
            latest.c.severity_band.label("a_severity_band"),
            latest.c.probable_origin.label("a_probable_origin"),
            latest.c.confidence.label("a_confidence"),
            latest.c.engine_version.label("a_engine_version"),
            latest.c.updated_at.label("a_updated_at"),
            latest.c.result["blast_radius"]["affected"].label("a_affected"),
        )
        .join(latest, (latest.c.tenant_id == d.tenant_id) & (latest.c.deployment_id == d.id))
        .where(d.tenant_id == ctx.tenant_id, latest.c.status.in_(REGRESSION_STATUSES))
    )
    if environment is not None:
        stmt = stmt.where(d.environment == environment)
    if service is not None:
        stmt = stmt.where(d.service == service)
    if after is not None:
        stmt = stmt.where(
            tuple_(d.deployed_at, d.id) < tuple_(after.deployed_at, after.deployment_id)
        )
    stmt = stmt.order_by(d.deployed_at.desc(), d.id.desc()).limit(limit)
    return (await session.execute(stmt)).all()


async def latest_environment_analysis(
    session: AsyncSession, ctx: TenantContext, environment: str
) -> RegressionAnalysisRecord | None:
    """The most complete analysis of the newest analysed deployment in ``environment``."""
    ra = RegressionAnalysisRecord
    stmt = (
        select(ra)
        .join(
            Deployment, (Deployment.tenant_id == ra.tenant_id) & (Deployment.id == ra.deployment_id)
        )
        .where(ra.tenant_id == ctx.tenant_id, Deployment.environment == environment)
        .order_by(Deployment.deployed_at.desc(), Deployment.id.desc(), ra.kind.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
