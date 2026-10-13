"""Deployments and their analysis jobs (tenant-scoped)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, true, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.events import DeploymentEvent
from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.analyses import RegressionAnalysisRecord
from rl_db.models.deployments import Deployment
from rl_db.models.jobs import DEFAULT_MAX_ATTEMPTS, AnalysisJob, JobKind
from rl_db.repositories._tenancy import require_tenant

# End of the post-deploy observation window (ARCHITECTURE.md §6: post = [T+2m, T+15m]).
DEFAULT_POST_WINDOW_END = timedelta(minutes=15)


def analysis_schedule(
    post_window_end: timedelta = DEFAULT_POST_WINDOW_END,
) -> tuple[tuple[JobKind, timedelta], ...]:
    """When each analysis job runs, relative to ``deployed_at``.

    The early looks are fixed at +5 and +10 minutes. The final verdict waits for the whole
    post-deploy window, so it must not run before the last early look.
    """
    if post_window_end < timedelta(minutes=10):
        raise ValueError("post_window_end must be at least 10 minutes (after early_10m)")
    return (
        (JobKind.EARLY_5M, timedelta(minutes=5)),
        (JobKind.EARLY_10M, timedelta(minutes=10)),
        (JobKind.FINAL, post_window_end),
    )


@dataclass(frozen=True, slots=True)
class RecordedDeployment:
    """Outcome of :func:`record_deployment`.

    ``created`` is False when the event was already recorded (a redelivery). Nothing was
    written then, and ``deployment_id`` is None.
    """

    created: bool
    deployment_id: uuid.UUID | None
    jobs_scheduled: int


async def record_deployment(
    session: AsyncSession,
    ctx: TenantContext,
    event: DeploymentEvent,
    *,
    schedule: Sequence[tuple[JobKind, timedelta]] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RecordedDeployment:
    """Insert the deployment and its analysis jobs, idempotently on ``(tenant, event_id)``.

    ``INSERT … ON CONFLICT DO NOTHING RETURNING id`` returns a row only when this call
    inserted it. Jobs are created only in that case, so a redelivered event writes nothing.
    Two consumers racing on the same event cannot both see a returned id, because the
    unique index serialises them. The caller commits.
    """
    require_tenant(ctx, event.tenant_id)
    insert_deployment = (
        pg_insert(Deployment)
        .values(
            id=uuid7(),
            tenant_id=event.tenant_id,
            event_id=event.event_id,
            schema_version=event.schema_version,
            service=event.service,
            environment=event.environment,
            version=event.version,
            previous_version=event.previous_version,
            commit_sha=event.commit_sha,
            deployer=event.deployer,
            ci_run_url=event.ci_run_url,
            repository=event.repository,
            pull_request=event.pull_request,
            risk_assessment_id=event.risk_assessment_id,
            labels=dict(event.labels),
            occurred_at=event.occurred_at,
            deployed_at=event.deployed_at,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "event_id"])
        .returning(Deployment.id)
    )
    deployment_id = (await session.execute(insert_deployment)).scalar_one_or_none()
    if deployment_id is None:
        return RecordedDeployment(created=False, deployment_id=None, jobs_scheduled=0)

    jobs = [
        {
            "id": uuid7(),
            "tenant_id": event.tenant_id,
            "deployment_id": deployment_id,
            "kind": kind,
            "run_at": event.deployed_at + offset,
            "max_attempts": max_attempts,
        }
        for kind, offset in (schedule if schedule is not None else analysis_schedule())
    ]
    if jobs:
        await session.execute(
            pg_insert(AnalysisJob)
            .values(jobs)
            .on_conflict_do_nothing(index_elements=["tenant_id", "deployment_id", "kind"])
        )
    return RecordedDeployment(created=True, deployment_id=deployment_id, jobs_scheduled=len(jobs))


async def get_deployment(
    session: AsyncSession, ctx: TenantContext, deployment_id: uuid.UUID
) -> Deployment | None:
    """The tenant's deployment, or None. Another tenant's id is indistinguishable from a
    missing one, so callers can answer 404 without leaking existence (ADR-0010 §4)."""
    stmt = select(Deployment).where(
        Deployment.tenant_id == ctx.tenant_id, Deployment.id == deployment_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class DeploymentCursor:
    """Keyset position: the last row of the previous page (newest first)."""

    deployed_at: datetime
    id: uuid.UUID


async def list_deployments(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int,
    service: str | None = None,
    environment: str | None = None,
    after: DeploymentCursor | None = None,
    since: datetime | None = None,
) -> list[Deployment]:
    """Newest first, ordered by ``(deployed_at, id)`` descending; ``since`` keeps deployments
    with ``deployed_at >= since``.

    Keyset pagination, rather than OFFSET, keeps every page an index range scan and
    stays stable while new deployments are being inserted.
    """
    stmt = _list_filter(
        select(Deployment),
        ctx,
        limit=limit,
        service=service,
        environment=environment,
        after=after,
        since=since,
    )
    return list((await session.execute(stmt)).scalars())


@dataclass(frozen=True, slots=True)
class LatestAnalysis:
    """The verdict columns of a deployment's most complete analysis (final > early_10m > …)."""

    id: uuid.UUID
    kind: JobKind
    status: str
    severity_score: float | None
    severity_band: str | None
    probable_origin: str | None
    confidence: str | None
    analysed_at: datetime


def _latest_analysis_lateral() -> Any:
    """``LATERAL`` subquery: the most complete analysis of the enclosing deployment row. One
    index probe per row on ``UNIQUE (tenant_id, deployment_id, kind)``, in the same query."""
    ra = RegressionAnalysisRecord
    return (
        select(
            ra.id.label("a_id"),
            ra.kind.label("a_kind"),
            ra.status.label("a_status"),
            ra.severity_score.label("a_severity_score"),
            ra.severity_band.label("a_severity_band"),
            ra.probable_origin.label("a_probable_origin"),
            ra.confidence.label("a_confidence"),
            ra.updated_at.label("a_updated_at"),
        )
        .where(ra.tenant_id == Deployment.tenant_id, ra.deployment_id == Deployment.id)
        .order_by(ra.kind.desc())
        .limit(1)
        .lateral("latest_analysis")
    )


def _with_latest(rows: Sequence[Any]) -> list[tuple[Deployment, LatestAnalysis | None]]:
    return [
        (
            r.Deployment,
            None
            if r.a_id is None
            else LatestAnalysis(
                id=r.a_id,
                kind=r.a_kind,
                status=r.a_status,
                severity_score=r.a_severity_score,
                severity_band=r.a_severity_band,
                probable_origin=r.a_probable_origin,
                confidence=r.a_confidence,
                analysed_at=r.a_updated_at,
            ),
        )
        for r in rows
    ]


async def list_deployments_with_latest_analysis(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int,
    service: str | None = None,
    environment: str | None = None,
    after: DeploymentCursor | None = None,
    since: datetime | None = None,
) -> list[tuple[Deployment, LatestAnalysis | None]]:
    """:func:`list_deployments`, each row with its latest analysis (or None) from one joined
    query (no query per row)."""
    latest = _latest_analysis_lateral()
    stmt = _list_filter(
        select(Deployment, latest).outerjoin(latest, true()),
        ctx,
        limit=limit,
        service=service,
        environment=environment,
        after=after,
        since=since,
    )
    return _with_latest((await session.execute(stmt)).all())


async def get_deployment_with_latest_analysis(
    session: AsyncSession, ctx: TenantContext, deployment_id: uuid.UUID
) -> tuple[Deployment, LatestAnalysis | None] | None:
    """:func:`get_deployment` with the deployment's latest analysis, in one query."""
    latest = _latest_analysis_lateral()
    stmt = (
        select(Deployment, latest)
        .outerjoin(latest, true())
        .where(Deployment.tenant_id == ctx.tenant_id, Deployment.id == deployment_id)
    )
    found = _with_latest((await session.execute(stmt)).all())
    return found[0] if found else None


def _list_filter[S: Select[Any]](
    stmt: S,
    ctx: TenantContext,
    *,
    limit: int,
    service: str | None,
    environment: str | None,
    after: DeploymentCursor | None,
    since: datetime | None,
) -> S:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be within 1..1000")
    stmt = stmt.where(Deployment.tenant_id == ctx.tenant_id)
    if service is not None:
        stmt = stmt.where(Deployment.service == service)
    if environment is not None:
        stmt = stmt.where(Deployment.environment == environment)
    if since is not None:
        stmt = stmt.where(Deployment.deployed_at >= since)
    if after is not None:
        stmt = stmt.where(
            tuple_(Deployment.deployed_at, Deployment.id) < tuple_(after.deployed_at, after.id)
        )
    return stmt.order_by(Deployment.deployed_at.desc(), Deployment.id.desc()).limit(limit)


async def list_analysis_jobs(
    session: AsyncSession, ctx: TenantContext, deployment_id: uuid.UUID
) -> list[AnalysisJob]:
    stmt = (
        select(AnalysisJob)
        .where(AnalysisJob.tenant_id == ctx.tenant_id, AnalysisJob.deployment_id == deployment_id)
        .order_by(AnalysisJob.run_at, AnalysisJob.kind)
    )
    return list((await session.execute(stmt)).scalars())
