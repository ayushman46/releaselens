"""JobRunner against real Postgres (test plan item g, job half).

This module uses a database of its own: the job queue spans tenants by design, so rows
left by other test modules would be claimed too.
"""

from __future__ import annotations

import asyncio
import collections
import sys
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import DeploymentEvent
from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.admin.jobs import ClaimedJob
from rl_db.models import AnalysisJob, JobKind, JobStatus
from rl_db.repositories import record_deployment
from rl_worker.jobs import HandlerRegistry, JobRunner
from rl_worker.retry import Backoff

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
def jobs_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_jobs")


@pytest.fixture
async def db(jobs_db_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(jobs_db_url, pool_size=8)
    sessions = create_session_factory(engine)
    async with sessions.begin() as s:  # every test starts from an empty queue
        await s.execute(AnalysisJob.__table__.delete())
    yield sessions
    await engine.dispose()


async def _due_deployments(
    sessions: Sessions, n: int, *, max_attempts: int = 5
) -> tuple[TenantContext, list[uuid.UUID]]:
    async with sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
    ctx = TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)
    ids = []
    past = datetime.now(UTC) - timedelta(hours=1)  # all three jobs already due
    async with sessions.begin() as s:
        for i in range(n):
            event = DeploymentEvent(
                event_id=uuid7(),
                tenant_id=org.id,
                occurred_at=past,
                schema_version=2,
                service=f"svc-{i}",
                environment="production",
                version="v2",
                previous_version="v1",
                commit_sha="abcdef1",
                deployed_at=past,
                deployer=None,
                ci_run_url=None,
                repository=None,
                pull_request=None,
                risk_assessment_id=None,
                labels=MappingProxyType({}),
            )
            rec = await record_deployment(s, ctx, event, max_attempts=max_attempts)
            assert rec.deployment_id is not None
            ids.append(rec.deployment_id)
    return ctx, ids


async def _statuses(sessions: Sessions) -> dict[tuple[JobKind, JobStatus], int]:
    async with sessions() as s:
        rows = await s.execute(
            select(AnalysisJob.kind, AnalysisJob.status, func.count()).group_by(
                AnalysisJob.kind, AnalysisJob.status
            )
        )
        return {(JobKind(k), JobStatus(st)): int(n) for k, st, n in rows.all()}


async def test_concurrent_runners_never_claim_the_same_job(db: Sessions) -> None:
    await _due_deployments(db, 30)  # 90 jobs; 60 of them have handlers
    handled: list[tuple[str, uuid.UUID]] = []

    def handler_for(runner_id: str):  # type: ignore[no-untyped-def]
        async def handle(job: ClaimedJob) -> None:
            handled.append((runner_id, job.id))
            await asyncio.sleep(0.005)  # yield, so the runners really interleave

        return handle

    runners = []
    for i in range(4):
        registry = HandlerRegistry()
        registry.register(JobKind.EARLY_5M, handler_for(f"r{i}"))
        registry.register(JobKind.FINAL, handler_for(f"r{i}"))
        runners.append(JobRunner(db, registry, worker_id=f"r{i}", batch_size=3))

    async def drain(runner: JobRunner) -> None:
        while await runner.run_once():
            pass

    started = time.perf_counter()
    await asyncio.gather(*(drain(r) for r in runners))
    elapsed = time.perf_counter() - started
    print(f"[measure] four_runners_drain_60_jobs={elapsed:.3f}s", file=sys.stderr)

    job_ids = [job_id for _, job_id in handled]
    assert len(job_ids) == 60
    assert len(set(job_ids)) == 60, "a job was claimed by more than one runner"
    per_runner = collections.Counter(runner_id for runner_id, _ in handled)
    assert len(per_runner) > 1  # the work really was shared
    statuses = await _statuses(db)
    assert statuses[(JobKind.EARLY_5M, JobStatus.SUCCEEDED)] == 30
    assert statuses[(JobKind.FINAL, JobStatus.SUCCEEDED)] == 30
    # No handler for early_10m: never claimed, never faked as done.
    assert statuses[(JobKind.EARLY_10M, JobStatus.PENDING)] == 30
    async with db() as s:
        untouched = await s.scalar(
            select(func.count())
            .select_from(AnalysisJob)
            .where(AnalysisJob.kind == JobKind.EARLY_10M, AnalysisJob.attempts == 0)
        )
    assert untouched == 30


async def test_runner_without_handlers_leaves_everything_pending(db: Sessions) -> None:
    await _due_deployments(db, 2)
    runner = JobRunner(db, HandlerRegistry(), worker_id="idle")
    assert await runner.run_once() == 0
    assert set(await _statuses(db)) == {(k, JobStatus.PENDING) for k in JobKind}


async def test_failures_back_off_then_die_after_max_attempts(db: Sessions) -> None:
    await _due_deployments(db, 1, max_attempts=2)
    calls = 0

    async def flaky(job: ClaimedJob) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"analysis failed on attempt {job.attempts}")

    registry = HandlerRegistry()
    registry.register(JobKind.FINAL, flaky)
    runner = JobRunner(
        db, registry, worker_id="w", backoff=Backoff(base_s=60.0, cap_s=60.0, max_attempts=2)
    )
    assert await runner.run_once() == 1
    async with db() as s:
        job = (await s.scalars(select(AnalysisJob).where(AnalysisJob.kind == JobKind.FINAL))).one()
        db_now = await s.scalar(select(func.now()))
    assert (job.status, job.attempts, job.locked_by) == (JobStatus.FAILED, 1, None)
    assert job.last_error == "RuntimeError: analysis failed on attempt 1"
    assert db_now is not None
    assert job.run_at > db_now  # pushed into the future by the backoff (full jitter, 0..60 s)

    assert await runner.run_once() == 0  # not due yet: the backoff is respected
    async with db.begin() as s:
        await s.execute(
            update(AnalysisJob).where(AnalysisJob.id == job.id).values(run_at=func.now())
        )
    assert await runner.run_once() == 1
    async with db() as s:
        job = (await s.scalars(select(AnalysisJob).where(AnalysisJob.id == job.id))).one()
    assert (job.status, job.attempts) == (JobStatus.DEAD, 2)
    assert calls == 2
    assert await runner.run_once() == 0  # dead jobs are never claimed again
    assert (runner.stats.failed, runner.stats.dead) == (1, 1)


async def test_handler_timeout_counts_as_a_failure(db: Sessions) -> None:
    await _due_deployments(db, 1)

    async def slow(_job: ClaimedJob) -> None:
        await asyncio.sleep(10)

    registry = HandlerRegistry()
    registry.register(JobKind.EARLY_5M, slow)
    runner = JobRunner(
        db, registry, worker_id="w", job_timeout=timedelta(seconds=0.2), lease=timedelta(minutes=1)
    )
    assert await runner.run_once() == 1
    async with db() as s:
        job = (
            await s.scalars(select(AnalysisJob).where(AnalysisJob.kind == JobKind.EARLY_5M))
        ).one()
    assert job.status == JobStatus.FAILED
    assert job.last_error is not None
    assert job.last_error.startswith("timed out")


async def test_a_timeout_inside_the_handler_is_not_reported_as_the_job_timeout(
    db: Sessions,
) -> None:
    """Seen live: a 15 s database statement timeout surfaced as "timed out after 300s"."""
    await _due_deployments(db, 1)

    async def statement_timeout(_job: ClaimedJob) -> None:
        raise TimeoutError

    registry = HandlerRegistry()
    registry.register(JobKind.EARLY_5M, statement_timeout)
    runner = JobRunner(
        db, registry, worker_id="w", job_timeout=timedelta(seconds=30), lease=timedelta(minutes=1)
    )
    assert await runner.run_once() == 1
    async with db() as s:
        job = (
            await s.scalars(select(AnalysisJob).where(AnalysisJob.kind == JobKind.EARLY_5M))
        ).one()
    assert job.status == JobStatus.FAILED
    assert job.last_error == "TimeoutError: no detail"


async def test_stop_hands_unstarted_jobs_back_without_using_an_attempt(db: Sessions) -> None:
    await _due_deployments(db, 3)
    ran: list[uuid.UUID] = []
    stop = asyncio.Event()

    async def first_then_stop(job: ClaimedJob) -> None:
        ran.append(job.id)
        stop.set()  # SIGTERM arrives while the first job of the batch runs

    registry = HandlerRegistry()
    registry.register(JobKind.FINAL, first_then_stop)
    runner = JobRunner(db, registry, worker_id="w", batch_size=10)
    assert await runner.run_once(stop) == 3
    assert len(ran) == 1
    async with db() as s:
        rows = list(await s.scalars(select(AnalysisJob).where(AnalysisJob.kind == JobKind.FINAL)))
    by_status = collections.Counter(r.status for r in rows)
    assert by_status == {JobStatus.SUCCEEDED: 1, JobStatus.PENDING: 2}
    assert all(r.attempts == 0 for r in rows if r.status == JobStatus.PENDING)
    assert runner.stats.released == 2
