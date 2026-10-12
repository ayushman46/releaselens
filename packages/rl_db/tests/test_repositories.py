"""Repositories and system queries against a real, migrated Postgres 16."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rl_core.events import DeploymentEvent
from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin import identity, jobs, outbox
from rl_db.models import AnalysisJob, ApiKey, Deployment, JobKind, JobStatus, OutboxEvent
from rl_db.repositories import (
    DeploymentCursor,
    add_outbox_event,
    analysis_schedule,
    get_deployment,
    insert_api_key,
    list_analysis_jobs,
    list_deployments,
    record_deployment,
    revoke_api_key,
)
from rl_db.repositories._tenancy import TenantMismatchError

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_url, pool_size=4)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    return create_session_factory(engine)


async def _tenant(sessions: Sessions) -> TenantContext:
    async with sessions.begin() as s:
        org = await identity.create_organization(
            s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:10]}"
        )
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)


def _event(ctx: TenantContext, **overrides: object) -> DeploymentEvent:
    fields: dict[str, object] = {
        "event_id": uuid7(),
        "tenant_id": ctx.tenant_id,
        "occurred_at": datetime(2026, 10, 13, 12, 0, 1, tzinfo=UTC),
        "schema_version": 2,
        "service": "payment",
        "environment": "production",
        "version": "v2",
        "previous_version": "v1",
        "commit_sha": "abcdef1",
        "deployed_at": datetime(2026, 10, 13, 12, tzinfo=UTC),
        "deployer": None,
        "ci_run_url": None,
        "repository": "acme/payment",
        "pull_request": 7,
        "risk_assessment_id": None,
        "labels": MappingProxyType({"team": "pay"}),
    }
    fields.update(overrides)
    return DeploymentEvent(**fields)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- deployments
async def test_record_deployment_is_idempotent_and_schedules_three_jobs(
    sessions: Sessions,
) -> None:
    ctx = await _tenant(sessions)
    event = _event(ctx)
    async with sessions.begin() as s:
        first = await record_deployment(s, ctx, event)
    async with sessions.begin() as s:
        second = await record_deployment(s, ctx, event)

    assert first.created
    assert first.jobs_scheduled == 3
    assert not second.created
    assert second.deployment_id is None
    async with sessions() as s:
        rows = await s.execute(select(Deployment).where(Deployment.tenant_id == ctx.tenant_id))
        deployments = list(rows.scalars())
        assert len(deployments) == 1
        assert deployments[0].labels == {"team": "pay"}
        job_rows = await list_analysis_jobs(s, ctx, deployments[0].id)
    assert {(j.kind, j.run_at - event.deployed_at) for j in job_rows} == {
        (JobKind.EARLY_5M, timedelta(minutes=5)),
        (JobKind.EARLY_10M, timedelta(minutes=10)),
        (JobKind.FINAL, timedelta(minutes=15)),
    }
    assert all(j.status == JobStatus.PENDING and j.attempts == 0 for j in job_rows)


async def test_concurrent_inserts_of_the_same_event_create_one_row(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    event = _event(ctx)

    async def insert() -> bool:
        async with sessions.begin() as s:
            return (await record_deployment(s, ctx, event)).created

    results = await asyncio.gather(*(insert() for _ in range(8)))
    assert sorted(results) == [False] * 7 + [True]
    async with sessions() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(AnalysisJob)
            .where(AnalysisJob.tenant_id == ctx.tenant_id)
        )
    assert count == 3


async def test_record_deployment_refuses_a_foreign_tenant_event(sessions: Sessions) -> None:
    ctx_a, ctx_b = await _tenant(sessions), await _tenant(sessions)
    async with sessions.begin() as s:
        with pytest.raises(TenantMismatchError):
            await record_deployment(s, ctx_a, _event(ctx_b))


async def test_jobs_cannot_reference_another_tenants_deployment(sessions: Sessions) -> None:
    ctx_a, ctx_b = await _tenant(sessions), await _tenant(sessions)
    async with sessions.begin() as s:
        recorded = await record_deployment(s, ctx_a, _event(ctx_a))
    with pytest.raises(IntegrityError, match="fk_analysis_jobs_tenant_id_deployments"):
        async with sessions.begin() as s:
            s.add(
                AnalysisJob(
                    tenant_id=ctx_b.tenant_id,
                    deployment_id=recorded.deployment_id,
                    kind=JobKind.FINAL,
                    run_at=datetime.now(UTC),
                )
            )


async def test_analysis_schedule_validates_post_window() -> None:
    assert analysis_schedule(timedelta(minutes=20))[-1] == (JobKind.FINAL, timedelta(minutes=20))
    with pytest.raises(ValueError, match="10 minutes"):
        analysis_schedule(timedelta(minutes=9))


async def test_get_and_list_are_tenant_scoped_and_paginate(sessions: Sessions) -> None:
    ctx_a, ctx_b = await _tenant(sessions), await _tenant(sessions)
    base = datetime(2026, 10, 1, tzinfo=UTC)
    async with sessions.begin() as s:
        ids_a = [
            (
                await record_deployment(
                    s,
                    ctx_a,
                    _event(
                        ctx_a,
                        deployed_at=base + timedelta(minutes=i // 2),  # pairs share a timestamp
                        service="payment" if i % 3 else "orders",
                    ),
                )
            ).deployment_id
            for i in range(7)
        ]
        b = await record_deployment(s, ctx_b, _event(ctx_b))

    async with sessions() as s:
        assert await get_deployment(s, ctx_b, ids_a[0]) is None  # other tenant: invisible
        assert b.deployment_id is not None
        assert (await get_deployment(s, ctx_b, b.deployment_id)) is not None

        seen: list[uuid.UUID] = []
        cursor: DeploymentCursor | None = None
        while True:
            page = await list_deployments(s, ctx_a, limit=3, after=cursor)
            if not page:
                break
            seen.extend(d.id for d in page)
            cursor = DeploymentCursor(page[-1].deployed_at, page[-1].id)
        assert len(seen) == 7
        assert set(seen) == set(ids_a)
        ordered = await list_deployments(s, ctx_a, limit=100)
        keys = [(d.deployed_at, d.id) for d in ordered]
        assert keys == sorted(keys, reverse=True)
        assert seen == [d.id for d in ordered]

        orders = await list_deployments(s, ctx_a, limit=100, service="orders")
        assert {d.service for d in orders} == {"orders"}
        assert len(orders) == 3
        assert await list_deployments(s, ctx_a, limit=10, environment="staging") == []


# ----------------------------------------------------------------------------- jobs
async def _due_jobs(sessions: Sessions, n_deployments: int) -> TenantContext:
    ctx = await _tenant(sessions)
    past = datetime.now(UTC) - timedelta(hours=1)
    async with sessions.begin() as s:
        for _ in range(n_deployments):
            await record_deployment(s, ctx, _event(ctx, deployed_at=past))
    return ctx


async def test_skip_locked_claims_are_disjoint(sessions: Sessions) -> None:
    ctx = await _due_jobs(sessions, 4)  # 12 due jobs
    kinds = list(JobKind)
    async with sessions() as s1, sessions() as s2:
        await s1.begin()
        await s2.begin()
        a = await jobs.claim_due_jobs(
            s1, worker_id="a", kinds=kinds, limit=5, lease=timedelta(minutes=5)
        )
        # s1 still holds its row locks; s2 must skip them rather than wait or double-claim.
        b = await asyncio.wait_for(
            jobs.claim_due_jobs(
                s2, worker_id="b", kinds=kinds, limit=100, lease=timedelta(minutes=5)
            ),
            timeout=5,
        )
        await s1.commit()
        await s2.commit()
    mine = {j.id for j in a} | {j.id for j in b}
    async with sessions() as s:
        all_ids = set(
            await s.scalars(select(AnalysisJob.id).where(AnalysisJob.tenant_id == ctx.tenant_id))
        )
    assert len(a) == 5
    assert {j.id for j in a}.isdisjoint({j.id for j in b})
    assert all_ids <= mine  # other tests' jobs may also be claimed; ours all are
    assert all(j.attempts == 1 for j in a + b)


async def test_unregistered_kinds_are_never_claimed(sessions: Sessions) -> None:
    ctx = await _due_jobs(sessions, 2)
    async with sessions.begin() as s:
        claimed = await jobs.claim_due_jobs(
            s, worker_id="w", kinds=[JobKind.FINAL], limit=100, lease=timedelta(minutes=5)
        )
    assert claimed
    assert {j.kind for j in claimed} == {JobKind.FINAL}
    async with sessions() as s:
        rows = (
            await s.execute(
                select(AnalysisJob.kind, AnalysisJob.status).where(
                    AnalysisJob.tenant_id == ctx.tenant_id, AnalysisJob.kind != JobKind.FINAL
                )
            )
        ).all()
    assert len(rows) == 4
    assert all(status == JobStatus.PENDING for _, status in rows)
    async with sessions.begin() as s:
        assert (
            await jobs.claim_due_jobs(
                s, worker_id="w", kinds=[], limit=100, lease=timedelta(minutes=5)
            )
            == []
        )


async def test_fail_then_dead_after_max_attempts(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    past = datetime.now(UTC) - timedelta(hours=1)
    async with sessions.begin() as s:
        rec = await record_deployment(
            s,
            ctx,
            _event(ctx, deployed_at=past),
            schedule=[(JobKind.FINAL, timedelta(0))],
            max_attempts=2,
        )
    lease = timedelta(minutes=5)

    async def claim_ours() -> jobs.ClaimedJob:
        async with sessions.begin() as s:
            # Claim with a large limit, then hand back every job that is not ours. That
            # keeps this test independent of jobs other tests left due.
            claimed = await jobs.claim_due_jobs(
                s, worker_id="w", kinds=[JobKind.FINAL], limit=1000, lease=lease
            )
            for job in claimed:
                if job.deployment_id != rec.deployment_id:
                    await jobs.release_job(s, job.id, "w")
        [job] = [j for j in claimed if j.deployment_id == rec.deployment_id]
        return job

    job = await claim_ours()
    async with sessions.begin() as s:
        status = await jobs.fail_job(s, job.id, "w", error="boom " * 1000, retry_delay=timedelta(0))
        assert status == JobStatus.FAILED
        # A runner that lost the claim cannot finish the job.
        assert not await jobs.complete_job(s, job.id, "someone-else")

    job = await claim_ours()
    assert job.attempts == 2
    async with sessions.begin() as s:
        assert (
            await jobs.fail_job(s, job.id, "w", error="boom", retry_delay=timedelta(0))
            == JobStatus.DEAD
        )
    async with sessions() as s:
        row = await s.get(AnalysisJob, job.id)
    assert row is not None
    assert row.status == JobStatus.DEAD
    assert row.locked_by is None
    assert row.last_error == "boom"


async def test_retry_pushes_run_at_and_release_keeps_attempts(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    async with sessions.begin() as s:
        rec = await record_deployment(
            s,
            ctx,
            _event(ctx, deployed_at=datetime.now(UTC) - timedelta(hours=1)),
            schedule=[(JobKind.EARLY_5M, timedelta(0))],
        )
        job_id = (
            await s.scalars(
                select(AnalysisJob.id).where(AnalysisJob.deployment_id == rec.deployment_id)
            )
        ).one()
        # Simulate a claim by "w" directly, so other tests' due jobs are not touched.
        await s.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == job_id)
            .values(status=JobStatus.RUNNING, locked_by="w", locked_at=func.now(), attempts=1)
        )
    async with sessions.begin() as s:
        assert await jobs.release_job(s, job_id, "w")
    async with sessions() as s:
        row = await s.get(AnalysisJob, job_id)
        assert row is not None
        assert row.status == JobStatus.PENDING
        assert row.attempts == 0
    async with sessions.begin() as s:
        await s.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == job_id)
            .values(status=JobStatus.RUNNING, locked_by="w", locked_at=func.now(), attempts=1)
        )
    async with sessions.begin() as s:
        before = await s.scalar(select(func.now()))
        assert (
            await jobs.fail_job(s, job_id, "w", error="x", retry_delay=timedelta(minutes=3))
            == JobStatus.FAILED
        )
    async with sessions() as s:
        row = await s.get(AnalysisJob, job_id)
    assert row is not None
    assert before is not None
    assert row.run_at >= before + timedelta(minutes=3) - timedelta(seconds=1)


async def test_expired_lease_on_last_attempt_is_reaped(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    async with sessions.begin() as s:
        rec = await record_deployment(
            s, ctx, _event(ctx), schedule=[(JobKind.FINAL, timedelta(0))], max_attempts=1
        )
        await s.execute(
            update(AnalysisJob)
            .where(AnalysisJob.deployment_id == rec.deployment_id)
            .values(
                status=JobStatus.RUNNING,
                locked_by="crashed",
                locked_at=func.now() - timedelta(hours=1),
                attempts=1,
            )
        )
    async with sessions.begin() as s:
        assert await jobs.reap_expired_leases(s, lease=timedelta(minutes=10)) >= 1
    async with sessions() as s:
        status = await s.scalar(
            select(AnalysisJob.status).where(AnalysisJob.deployment_id == rec.deployment_id)
        )
    assert status == JobStatus.DEAD


# ----------------------------------------------------------------------------- outbox
async def test_outbox_rows_commit_with_the_transaction(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    payload = {"tenant_id": str(ctx.tenant_id), "n": 1}

    async def write_then_fail() -> None:
        async with sessions.begin() as s:
            await add_outbox_event(s, ctx, "analysis.results", "k", payload)
            raise RuntimeError("domain write failed")

    with pytest.raises(RuntimeError):
        await write_then_fail()
    async with sessions.begin() as s:
        row_id = await add_outbox_event(
            s, ctx, "analysis.results", "k", payload, {"rl-kind": "test"}
        )
    async with sessions() as s:
        rows = list(
            await s.scalars(select(OutboxEvent).where(OutboxEvent.tenant_id == ctx.tenant_id))
        )
    assert [r.id for r in rows] == [row_id]
    assert rows[0].payload == payload
    assert rows[0].headers == {"rl-kind": "test"}


async def test_outbox_validates_input(sessions: Sessions) -> None:
    ctx, other = await _tenant(sessions), await _tenant(sessions)
    async with sessions.begin() as s:
        with pytest.raises(ValueError, match="tenant_id"):
            await add_outbox_event(s, ctx, "t", "k", {"x": 1})
        with pytest.raises(TenantMismatchError):
            await add_outbox_event(s, ctx, "t", "k", {"tenant_id": str(other.tenant_id)})
        with pytest.raises(ValueError, match="topic"):
            await add_outbox_event(s, ctx, "", "k", {"tenant_id": str(ctx.tenant_id)})


async def test_outbox_claim_skips_locked_and_marks(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    async with sessions.begin() as s:
        for i in range(3):
            await add_outbox_event(s, ctx, "t", f"k{i}", {"tenant_id": str(ctx.tenant_id)})
    async with sessions() as s1, sessions() as s2:
        await s1.begin()
        await s2.begin()
        a = await outbox.claim_unpublished(s1, limit=1000)
        b = await asyncio.wait_for(outbox.claim_unpublished(s2, limit=1000), timeout=5)
        assert {r.id for r in a}.isdisjoint({r.id for r in b})
        ours = [r for r in a if r.tenant_id == ctx.tenant_id]
        assert [r.key for r in ours] == ["k0", "k1", "k2"]  # oldest first
        await outbox.mark_failed(s1, ours[0].id, "broker down")
        assert await outbox.mark_published(s1, [r.id for r in ours[1:]]) == 2
        await s1.commit()
        await s2.rollback()
    async with sessions() as s:
        rows = {
            r.key: r
            for r in await s.scalars(
                select(OutboxEvent).where(OutboxEvent.tenant_id == ctx.tenant_id)
            )
        }
    assert rows["k0"].published_at is None
    assert rows["k0"].attempts == 1
    assert rows["k0"].last_error == "broker down"
    assert rows["k1"].published_at is not None
    assert rows["k1"].attempts == 1


async def test_partial_outbox_index_definition(engine: AsyncEngine) -> None:
    # Alembic's autogenerate diff does not compare partial-index predicates, so check it here.
    async with engine.connect() as conn:
        definition = await conn.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_outbox_unpublished'")
        )
    assert definition is not None
    assert "WHERE (published_at IS NULL)" in definition


# ----------------------------------------------------------------------------- api keys
async def test_api_key_lookup_touch_and_revoke(sessions: Sessions) -> None:
    ctx, other = await _tenant(sessions), await _tenant(sessions)
    prefix = uuid.uuid4().hex[:12]
    async with sessions.begin() as s:
        key = await insert_api_key(
            s, ctx, name="ci", prefix=prefix, secret_hash="h", scopes=["read", "read"]
        )
    async with sessions() as s:
        cred = await identity.find_api_key_by_prefix(s, prefix)
        assert await identity.find_api_key_by_prefix(s, "nope") is None
    assert cred is not None
    assert cred.tenant_id == ctx.tenant_id
    assert cred.scopes == frozenset({"read"})

    now = datetime.now(UTC)
    minute = timedelta(minutes=1)
    async with sessions.begin() as s:
        assert await identity.touch_api_key(s, key.id, now=now, min_interval=minute)
        assert not await identity.touch_api_key(
            s, key.id, now=now + timedelta(seconds=30), min_interval=minute
        )
        assert await identity.touch_api_key(s, key.id, now=now + minute, min_interval=minute)

    async with sessions.begin() as s:
        assert not await revoke_api_key(s, other, key.id)  # another tenant cannot revoke it
        assert await revoke_api_key(s, ctx, key.id)
        assert not await revoke_api_key(s, ctx, key.id)  # already revoked
    async with sessions() as s:
        row = await s.get(ApiKey, key.id)
    assert row is not None
    assert row.revoked_at is not None
    assert row.last_used_at == now + minute


async def test_unknown_scopes_are_rejected_by_the_database(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    with pytest.raises(IntegrityError, match="ck_api_keys_scopes_known"):
        async with sessions.begin() as s:
            await insert_api_key(
                s, ctx, name="x", prefix=uuid.uuid4().hex[:12], secret_hash="h", scopes=["admin"]
            )
