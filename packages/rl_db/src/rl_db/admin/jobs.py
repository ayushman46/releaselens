"""Analysis-job queue operations for the job runner (cross-tenant poll, ADR-0011).

Claiming is a single short transaction: ``UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP
LOCKED)``. Concurrent runners skip rows another runner has locked instead of waiting on
them, so two runners never claim the same job and never block each other. The claim commits
*before* the handler runs, so a long analysis holds no row locks. Each job's lease is
restarted when its handler starts (:func:`renew_lease`), and a crashed runner's job is
taken back after ``lease`` expires.

All time comparisons use the database clock (``now()``), so runners with skewed clocks agree
on which jobs are due.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, case, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.tenancy import Principal, TenantContext
from rl_db.models.jobs import AnalysisJob, JobKind, JobStatus
from rl_db.repositories._tenancy import cross_tenant

MAX_ERROR_LEN = 2000


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    tenant_id: uuid.UUID
    deployment_id: uuid.UUID
    kind: JobKind
    run_at: datetime
    attempts: int  # including this one
    max_attempts: int
    locked_by: str

    def tenant_context(self) -> TenantContext:
        """The context a handler uses for its tenant-scoped reads and writes."""
        return TenantContext(tenant_id=self.tenant_id, principal=Principal.SYSTEM)


@cross_tenant
async def claim_due_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    kinds: Collection[JobKind],
    limit: int,
    lease: timedelta,
) -> list[ClaimedJob]:
    """Claim up to ``limit`` due jobs of the given kinds and mark them ``running``.

    A job is due when it is pending or failed (waiting for its retry) and ``run_at`` has
    passed, or when it is running under an expired lease. Kinds without a registered
    handler are never claimed: they stay pending until a handler exists, rather than being
    marked done by a runner that cannot do them. The caller commits.
    """
    if not kinds or limit < 1:
        return []
    due_ids = (
        select(AnalysisJob.id)
        .where(
            AnalysisJob.kind.in_(list(kinds)),
            AnalysisJob.attempts < AnalysisJob.max_attempts,
            or_(
                and_(
                    AnalysisJob.status.in_([JobStatus.PENDING, JobStatus.FAILED]),
                    AnalysisJob.run_at <= func.now(),
                ),
                and_(
                    AnalysisJob.status == JobStatus.RUNNING,
                    AnalysisJob.locked_at < func.now() - lease,
                ),
            ),
        )
        .order_by(AnalysisJob.run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(AnalysisJob)
        .where(AnalysisJob.id.in_(due_ids.scalar_subquery()))
        .values(
            status=JobStatus.RUNNING,
            locked_by=worker_id,
            locked_at=func.now(),
            attempts=AnalysisJob.attempts + 1,
            updated_at=func.now(),
        )
        .returning(
            AnalysisJob.id,
            AnalysisJob.tenant_id,
            AnalysisJob.deployment_id,
            AnalysisJob.kind,
            AnalysisJob.run_at,
            AnalysisJob.attempts,
            AnalysisJob.max_attempts,
        )
    )
    rows = (await session.execute(stmt)).all()
    jobs = [
        ClaimedJob(
            id=r.id,
            tenant_id=r.tenant_id,
            deployment_id=r.deployment_id,
            kind=JobKind(r.kind),
            run_at=r.run_at,
            attempts=r.attempts,
            max_attempts=r.max_attempts,
            locked_by=worker_id,
        )
        for r in rows
    ]
    return sorted(jobs, key=lambda j: (j.run_at, j.id))


def _owned(job_id: uuid.UUID, worker_id: str) -> ColumnElement[bool]:
    # Only the runner that holds the claim may finish it. If the lease expired and another
    # runner re-claimed the job, the first runner's late result is discarded.
    return and_(
        AnalysisJob.id == job_id,
        AnalysisJob.status == JobStatus.RUNNING,
        AnalysisJob.locked_by == worker_id,
    )


@cross_tenant
async def renew_lease(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
    """Restart the lease clock of a claimed job just before its handler runs.

    A runner claims a batch, then runs the jobs one after another, so ``locked_at`` from the
    claim is stale for every job but the first. Without a renewal, a job waiting behind
    slow siblings would look abandoned, and another runner would re-claim it while it is
    running here. Renewed at start, a job keeps its claim for a full lease, and the runner's
    ``job_timeout < lease`` rule guarantees it finishes inside that. Returns False when the
    claim was already lost; the caller must then skip the job.
    """
    stmt = (
        update(AnalysisJob)
        .where(_owned(job_id, worker_id))
        .values(locked_at=func.now(), updated_at=func.now())
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def complete_job(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
    stmt = (
        update(AnalysisJob)
        .where(_owned(job_id, worker_id))
        .values(
            status=JobStatus.SUCCEEDED,
            locked_by=None,
            locked_at=None,
            last_error=None,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def fail_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    worker_id: str,
    *,
    error: str,
    retry_delay: timedelta,
) -> JobStatus | None:
    """Record a failed attempt: ``failed`` with ``run_at`` pushed back by ``retry_delay``, or
    ``dead`` once the job has used ``max_attempts``. Returns the new status, or None if the
    claim was lost."""
    exhausted = AnalysisJob.attempts >= AnalysisJob.max_attempts
    # Typed literals: a bare string in CASE is ``text`` to Postgres, not the enum.
    status_type = AnalysisJob.__table__.c.status.type
    stmt = (
        update(AnalysisJob)
        .where(_owned(job_id, worker_id))
        .values(
            status=case(
                (exhausted, literal(JobStatus.DEAD, status_type)),
                else_=literal(JobStatus.FAILED, status_type),
            ),
            run_at=case((exhausted, AnalysisJob.run_at), else_=func.now() + retry_delay),
            locked_by=None,
            locked_at=None,
            last_error=error[:MAX_ERROR_LEN],
            updated_at=func.now(),
        )
        .returning(AnalysisJob.status)
    )
    status = (await session.execute(stmt)).scalar_one_or_none()
    return JobStatus(status) if status is not None else None


@cross_tenant
async def release_job(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
    """Hand a claimed job back without using up an attempt (graceful shutdown)."""
    stmt = (
        update(AnalysisJob)
        .where(_owned(job_id, worker_id))
        .values(
            status=JobStatus.PENDING,
            attempts=AnalysisJob.attempts - 1,
            locked_by=None,
            locked_at=None,
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def reap_expired_leases(session: AsyncSession, *, lease: timedelta) -> int:
    """Mark ``dead`` the running jobs whose lease expired after their last allowed attempt.

    :func:`claim_due_jobs` re-claims expired leases only while attempts remain; without this
    sweep, a job whose last attempt crashed its worker would stay ``running`` forever.
    """
    stmt = (
        update(AnalysisJob)
        .where(
            AnalysisJob.status == JobStatus.RUNNING,
            AnalysisJob.locked_at < func.now() - lease,
            AnalysisJob.attempts >= AnalysisJob.max_attempts,
        )
        .values(
            status=JobStatus.DEAD,
            locked_by=None,
            locked_at=None,
            last_error="lease expired on the final attempt (worker crashed or timed out)",
            updated_at=func.now(),
        )
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0))
