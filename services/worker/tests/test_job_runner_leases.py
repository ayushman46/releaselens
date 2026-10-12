"""Adversarial JobRunner cases: leases, cancellation and SKIP LOCKED under heavy contention.

A job must never run on two runners at once while its first runner is still healthy, and a
runner that goes away must hand back everything it claimed but did not start. Uses a
database of its own, because the job queue spans tenants by design.
"""

from __future__ import annotations

import asyncio
import collections
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from sqlalchemy import delete, select
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

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
def leases_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_leases")


@pytest.fixture
async def db(leases_db_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(leases_db_url, pool_size=10)
    sessions = create_session_factory(engine)
    async with sessions.begin() as s:
        await s.execute(delete(AnalysisJob))
    yield sessions
    await engine.dispose()


async def _due_jobs(sessions: Sessions, deployments: int) -> list[uuid.UUID]:
    """``deployments`` deployments whose three jobs are all due now."""
    async with sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
    ctx = TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)
    past = datetime.now(UTC) - timedelta(hours=1)
    async with sessions.begin() as s:
        for i in range(deployments):
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
            await record_deployment(s, ctx, event)
    async with sessions() as s:
        return list(await s.scalars(select(AnalysisJob.id)))


def _registry(handler: object) -> HandlerRegistry:
    registry = HandlerRegistry()
    for kind in JobKind:
        registry.register(kind, handler)  # type: ignore[arg-type]
    return registry


async def test_jobs_queued_behind_a_slow_one_in_a_batch_never_run_twice(db: Sessions) -> None:
    """Runner A claims a batch of three; each job takes most of a lease. The lease must be
    counted from when a job *starts*, not from when its batch was claimed. Otherwise runner
    B re-claims a job A is still running (or about to run) and both execute it."""
    await _due_jobs(db, 1)
    runs: collections.Counter[uuid.UUID] = collections.Counter()
    running_now: set[uuid.UUID] = set()
    overlaps: list[uuid.UUID] = []

    async def slow(job: ClaimedJob) -> None:
        if job.id in running_now:
            overlaps.append(job.id)
        running_now.add(job.id)
        runs[job.id] += 1
        try:
            await asyncio.sleep(0.9)
        finally:
            running_now.discard(job.id)

    lease, timeout = timedelta(seconds=1.5), timedelta(seconds=1.2)
    a = JobRunner(
        db, _registry(slow), worker_id="a", batch_size=3, lease=lease, job_timeout=timeout
    )
    b = JobRunner(
        db, _registry(slow), worker_id="b", batch_size=3, lease=lease, job_timeout=timeout
    )
    a_done = asyncio.Event()

    async def run_a() -> None:
        try:
            assert await a.run_once() == 3
        finally:
            a_done.set()

    async def run_b() -> None:
        await asyncio.sleep(1.6)  # past the lease measured from A's claim
        while not a_done.is_set():
            await b.run_once()
            await asyncio.sleep(0.05)
        while await b.run_once():
            pass

    await asyncio.gather(run_a(), run_b())

    assert overlaps == [], "a job ran on two runners at the same time"
    assert sorted(runs.values()) == [1, 1, 1], f"some job ran more than once: {runs}"
    async with db() as s:
        statuses = list(await s.scalars(select(AnalysisJob.status)))
    assert statuses == [JobStatus.SUCCEEDED] * 3


async def test_a_cancelled_runner_hands_back_its_whole_batch(db: Sessions) -> None:
    """Cancellation (the supervisor's last resort after the shutdown timeout) must release
    the job that was running *and* the claimed jobs that never started, without using up
    their attempts. Left alone they would sit in ``running`` until the lease expired."""
    await _due_jobs(db, 1)
    started = asyncio.Event()

    async def hang(_job: ClaimedJob) -> None:
        started.set()
        await asyncio.sleep(60)

    runner = JobRunner(db, _registry(hang), worker_id="w", batch_size=3)
    task = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with db() as s:
        rows = list(await s.scalars(select(AnalysisJob)))
    assert [(r.status, r.attempts, r.locked_by) for r in rows] == [(JobStatus.PENDING, 0, None)] * 3
    assert runner.stats.released == 3


async def test_skip_locked_claims_stay_disjoint_under_heavy_contention(db: Sessions) -> None:
    """Eight runners with random batch sizes race over 300 due jobs: every job runs exactly
    once, on one attempt, and nothing is left behind."""
    job_ids = await _due_jobs(db, 100)
    assert len(job_ids) == 300
    handled: list[tuple[str, uuid.UUID]] = []
    rng = random.Random(7)

    def handler_for(runner_id: str):  # type: ignore[no-untyped-def]
        async def handle(job: ClaimedJob) -> None:
            handled.append((runner_id, job.id))
            await asyncio.sleep(rng.random() * 0.003)

        return handle

    runners = [
        JobRunner(
            db,
            _registry(handler_for(f"r{i}")),
            worker_id=f"r{i}",
            batch_size=rng.randint(1, 7),
        )
        for i in range(8)
    ]

    async def drain(runner: JobRunner) -> None:
        while await runner.run_once():
            pass

    await asyncio.gather(*(drain(r) for r in runners))

    counts = collections.Counter(job_id for _, job_id in handled)
    assert set(counts) == set(job_ids)
    assert set(counts.values()) == {1}, "a job was claimed by more than one runner"
    assert len({runner_id for runner_id, _ in handled}) > 1
    async with db() as s:
        rows = list(await s.execute(select(AnalysisJob.status, AnalysisJob.attempts)))
    assert {(status, attempts) for status, attempts in rows} == {(JobStatus.SUCCEEDED, 1)}
