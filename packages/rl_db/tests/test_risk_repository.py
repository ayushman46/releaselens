"""M5 persistence against a real, migrated Postgres 16: CI-run ingestion idempotency, the SQL
history window (must equal rl_core's pure ``bounded_history``), risk assessments, GitHub
installations and webhook deliveries."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rl_core.features.online import PastRun, bounded_history, compute_online
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin import github as gh_admin
from rl_db.admin.identity import create_organization
from rl_db.repositories import alerts, risk
from rl_db.repositories import github as gh
from rl_db.repositories import projects as catalog

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
BASE = datetime(2026, 1, 5, tzinfo=UTC)
SHA = "a" * 40


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
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:10]}")
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)


def _run(i: int, **kw: object) -> risk.CiRunWrite:
    values: dict[str, object] = {
        "repository": "acme/payments",
        "commit_sha": SHA,
        "started_at": BASE + timedelta(hours=i),
        "finished_at": BASE + timedelta(hours=i, minutes=10),
        "status": "passed",
        "source": "github_app",
        "external_id": f"gha:{i}:1",
    }
    values.update(kw)
    return risk.CiRunWrite(**values)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- ci_runs
async def test_ci_run_upsert_is_idempotent_and_completes_running_runs(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    async with sessions.begin() as s:
        first = await risk.upsert_ci_run(s, ctx, _run(1))
        again = await risk.upsert_ci_run(s, ctx, _run(1, status="failed"))
        running = await risk.upsert_ci_run(s, ctx, _run(2, status="running", finished_at=None))
        done = await risk.upsert_ci_run(s, ctx, _run(2, status="failed"))
        stale = await risk.upsert_ci_run(s, ctx, _run(2, status="running", finished_at=None))
        other_source = await risk.upsert_ci_run(s, ctx, _run(1, source="api"))
    assert first.outcome == "created"
    assert again.outcome == "duplicate"  # an outcome is never overwritten
    assert again.id == first.id
    assert running.outcome == "created"
    assert done.outcome == "updated"
    assert stale.outcome == "duplicate"  # out-of-order "running" does not undo the outcome
    assert other_source.outcome == "duplicate"  # same run reported by another source
    async with sessions() as s:
        rows = await risk.list_ci_runs(s, ctx, "acme/payments")
    assert sorted((r.external_id, r.status, r.source) for r in rows) == [
        ("gha:1:1", "passed", "github_app"),
        ("gha:2:1", "failed", "github_app"),
    ]
    assert {r.duration_s for r in rows} == {600.0}


async def test_ci_run_write_validates_its_input() -> None:
    with pytest.raises(ValueError, match="status"):
        _run(1, status="green")
    with pytest.raises(ValueError, match="lower-case"):
        _run(1, repository="Acme/Payments")
    with pytest.raises(ValueError, match="timezone"):
        _run(1, started_at=datetime(2026, 1, 1))


async def test_sql_history_window_equals_rl_core_bounded_history(sessions: Sessions) -> None:
    """The serving query and the pure function must produce identical feature vectors."""
    ctx = await _tenant(sessions)
    rng = np.random.default_rng(3)
    runs: list[PastRun] = []
    writes: list[risk.CiRunWrite] = []
    for i in range(160):
        start = BASE + timedelta(minutes=int(rng.integers(0, 60 * 24 * 20)))
        kind = rng.random()
        if kind < 0.05:
            status, finish = "running", None
        else:
            finish = start + timedelta(minutes=int(rng.integers(1, 4)) * 10)  # ties likely
            status = (
                "canceled"
                if kind < 0.1
                else ("errored" if kind < 0.15 else rng.choice(["passed", "failed"], p=[0.7, 0.3]))
            )
        writes.append(
            _run(0, started_at=start, finished_at=finish, status=str(status), external_id=f"r{i}")
        )
    async with sessions.begin() as s:
        for w in writes:  # insertion order = id order (UUIDv7) = the pure function's input order
            await risk.upsert_ci_run(s, ctx, w)
    for w in writes:
        failed = None if w.status in ("running", "canceled") else w.status != "passed"
        dur = (w.finished_at - w.started_at).total_seconds() if w.finished_at else None
        runs.append(PastRun(w.started_at, w.finished_at, failed, dur))

    queries = [BASE + timedelta(minutes=int(m)) for m in rng.integers(0, 60 * 24 * 21, 25)]
    for limit in (20, 30):
        for t in queries:
            async with sessions() as s:
                window, totals = await risk.load_history(s, ctx, "acme/payments", t, limit=limit)
            pure_window, pure_totals = bounded_history(runs, t, limit)
            assert totals == pure_totals, t
            facts = {"src_churn": 5}
            sql = compute_online(facts, window, t, totals=totals)
            assert sql == compute_online(facts, pure_window, t, totals=pure_totals), t
            assert sql == compute_online(facts, runs, t), t


async def test_history_is_tenant_and_repository_scoped(sessions: Sessions) -> None:
    a, b = await _tenant(sessions), await _tenant(sessions)
    async with sessions.begin() as s:
        await risk.upsert_ci_run(s, a, _run(1))
        await risk.upsert_ci_run(s, a, _run(2, repository="acme/other"))
    async with sessions() as s:
        _, mine = await risk.load_history(s, a, "acme/payments", BASE + timedelta(days=1))
        _, theirs = await risk.load_history(s, b, "acme/payments", BASE + timedelta(days=1))
    assert mine.eligible == 1
    assert theirs.eligible == 0


# ----------------------------------------------------------------------------- assessments
def _assessment(**kw: object) -> dict[str, object]:
    values: dict[str, object] = {
        "repository": "acme/payments",
        "commit_sha": SHA,
        "event": "pull_request",
        "pull_request": 7,
        "triggered_at": BASE,
        "features": {"src_churn": 3.0, "history_size": None},
        "change_facts": {"src_churn": 3},
        "model_name": "release_risk_ci",
        "model_version": "release_risk_ci/3",
        "feature_schema_version": "1.0.0",
        "raw_score": 0.5,
        "risk_score": 50,
        "risk_band": "MEDIUM",
        "probability": None,
        "shap": {"values": {}},
        "history_used": 0,
        "cold_start": True,
    }
    values.update(kw)
    return values


async def test_assessments_are_tenant_scoped_and_found_for_idempotency(
    sessions: Sessions,
) -> None:
    a, b = await _tenant(sessions), await _tenant(sessions)
    async with sessions.begin() as s:
        row = await risk.insert_assessment(s, a, _assessment())
        other = await risk.insert_assessment(s, a, _assessment(pull_request=None, event="push"))
    async with sessions() as s:
        assert (await risk.get_assessment(s, a, row.id)) is not None
        assert (await risk.get_assessment(s, b, row.id)) is None  # cross-tenant looks missing
        since = datetime.now(UTC) - timedelta(minutes=10)
        found = await risk.find_recent_assessment(
            s, a, repository="acme/payments", commit_sha=SHA, event="pull_request",
            pull_request=7, since=since,
        )  # fmt: skip
        push = await risk.find_recent_assessment(
            s, a, repository="acme/payments", commit_sha=SHA, event="push",
            pull_request=None, since=since,
        )  # fmt: skip
        none = await risk.find_recent_assessment(
            s, b, repository="acme/payments", commit_sha=SHA, event="pull_request",
            pull_request=7, since=since,
        )  # fmt: skip
        future = await risk.find_recent_assessment(
            s, a, repository="acme/payments", commit_sha=SHA, event="pull_request",
            pull_request=7, since=datetime.now(UTC) + timedelta(minutes=1),
        )  # fmt: skip
        page = await risk.list_assessments(s, a, limit=1)
        rest = await risk.list_assessments(
            s, a, limit=5, after=risk.AssessmentCursor(page[0].created_at, page[0].id)
        )
        assert await risk.list_assessments(s, b, limit=5) == []
    assert found is not None
    assert found.id == row.id
    assert push is not None
    assert push.id == other.id
    assert none is None
    assert future is None
    assert [r.id for r in page + rest] == sorted([row.id, other.id], reverse=True)
    with pytest.raises(ValueError, match="tenant_id"):
        async with sessions.begin() as s:
            await risk.insert_assessment(s, a, _assessment(tenant_id=b.tenant_id))


async def test_notify_risk_finds_the_project_by_repository(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    async with sessions.begin() as s:
        project = await catalog.create_project(
            s, ctx, name="Payments", slug="payments", github_owner="Acme", github_repo="Payments"
        )
        await alerts.create_alert_rule(
            s,
            ctx,
            project_id=project.id,
            values={"name": "risk", "on_risk": True, "email_recipients": ["oncall@example.com"]},
        )
        row = await risk.insert_assessment(s, ctx, _assessment(risk_band="HIGH", risk_score=88))
        medium = await alerts.notify_risk(
            s, ctx, risk_assessment_id=row.id, band="MEDIUM", repository="acme/payments"
        )
        high = await alerts.notify_risk(
            s, ctx, risk_assessment_id=row.id, band="HIGH", repository="acme/payments",
            risk_score=88, details_url="https://app.example/app/risk/x",
        )  # fmt: skip
        unknown = await alerts.notify_risk(
            s, ctx, risk_assessment_id=row.id, band="CRITICAL", repository="acme/unknown"
        )
    assert (medium.created, high.created, unknown.created) == (0, 1, 0)


# ----------------------------------------------------------------------------- GitHub App
async def test_installations_deliveries_and_pull_requests(sessions: Sessions) -> None:
    a, b = await _tenant(sessions), await _tenant(sessions)
    inst = int(uuid.uuid4().int % 10**9) + 1
    async with sessions.begin() as s:
        await gh_admin.link_installation(
            s, installation_id=inst, tenant_id=a.tenant_id, account_login="Acme"
        )
        again = await gh_admin.link_installation(
            s, installation_id=inst, tenant_id=a.tenant_id, account_login="acme"
        )
    assert again.account_login == "acme"
    with pytest.raises(gh_admin.InstallationLinkedElsewhereError):
        async with sessions.begin() as s:
            await gh_admin.link_installation(
                s, installation_id=inst, tenant_id=b.tenant_id, account_login="acme"
            )
    async with sessions.begin() as s:
        found = await gh_admin.find_installation(s, inst)
        assert found is not None
        assert found.tenant_id == a.tenant_id
        assert await gh.installation_for_repository(s, a, "ACME/payments") is not None
        assert await gh.installation_for_repository(s, b, "acme/payments") is None
        delivery = f"d-{uuid.uuid4()}"
        assert await gh_admin.record_delivery(
            s, delivery_id=delivery, event="push", installation_id=inst
        )
        assert not await gh_admin.record_delivery(
            s, delivery_id=delivery, event="push", installation_id=inst
        )
        await gh_admin.set_delivery_outcome(s, delivery, outcome="ok", tenant_id=a.tenant_id)
        assert await gh_admin.set_installation_suspended(s, inst, suspended_at=datetime.now(UTC))
        assert await gh.installation_for_repository(s, a, "acme/payments") is None
        pr = gh.PullRequestWrite(
            repository="acme/payments", number=4, head_sha=SHA, base_sha=None, base_ref="main",
            state="open", merged=False, draft=False, github_updated_at=BASE,
        )  # fmt: skip
        await gh.upsert_pull_request(s, a, pr)
        closed = replace(
            pr, state="closed", merged=True, github_updated_at=BASE + timedelta(hours=1)
        )
        await gh.upsert_pull_request(s, a, closed)
        older = replace(pr, head_sha="b" * 40, github_updated_at=BASE - timedelta(hours=1))
        await gh.upsert_pull_request(s, a, older)  # an older event never wins
        stored = await gh.get_pull_request(s, a, "acme/payments", 4)
        assert await gh.get_pull_request(s, b, "acme/payments", 4) is None
    assert stored is not None
    assert (stored.state, stored.merged, stored.head_sha) == ("closed", True, SHA)
