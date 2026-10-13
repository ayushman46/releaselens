"""Span persistence, edge/rollup derivation and analysis reads against real Postgres."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import DeploymentEvent
from rl_core.ids import uuid7
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind, StatusCode, datetime_to_ns
from rl_core.telemetry.rollup import LATENCY_BUCKETS, bucket_index
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.admin.partitions import ensure_partitions_for_days
from rl_db.admin.telemetry import delete_rollups_older_than, existing_tenants
from rl_db.models import JobKind, RegressionAnalysisRecord, ServiceEdge, ServiceMinuteRollup
from rl_db.repositories import (
    AnalysisWrite,
    get_deployment_with_latest_analysis,
    latest_analysis_windows,
    latest_environment_analysis,
    list_analyses,
    list_deployments_with_latest_analysis,
    list_regressions,
    load_analysis_spans,
    record_deployment,
    record_spans,
    upsert_analysis,
)
from rl_db.repositories.analyses import sample_threshold
from rl_db.repositories.catalog import list_environments, minute_series
from rl_db.repositories.stats import overview_counts
from rl_db.repositories.telemetry import fold_new_spans, insert_spans

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
BASE = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
BASE_NS = datetime_to_ns(BASE)


@pytest.fixture
async def sessions(migrated_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(migrated_url, pool_size=4)
    factory = create_session_factory(engine)
    async with factory.begin() as s:
        await ensure_partitions_for_days(s, [date(2026, 10, 12), date(2026, 10, 13)])
    yield factory
    await engine.dispose()


@pytest.fixture
async def ctx(sessions: Sessions) -> TenantContext:
    async with sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)


def _span(
    ctx: TenantContext,
    trace: int,
    span: int,
    parent: int | None,
    service: str,
    kind: SpanKind = SpanKind.SERVER,
    *,
    start_ms: float = 0.0,
    dur_ms: float = 10.0,
    env: str | None = "demo",
    error: bool = False,
) -> Span:
    start = BASE_NS + round(start_ms * NS_PER_MS)
    return Span(
        tenant_id=str(ctx.tenant_id),
        trace_id=f"{trace:032x}",
        span_id=f"{span:016x}",
        parent_span_id=None if parent is None else f"{parent:016x}",
        service=service,
        service_version="v1",
        environment=env,
        name=f"{service}-{span}",
        kind=kind,
        start_ns=start,
        end_ns=start + round(dur_ms * NS_PER_MS),
        status_code=StatusCode.ERROR if error else StatusCode.UNSET,
        attributes=MappingProxyType({"k": "v", "n": 1}),
    )


def _call(ctx: TenantContext, trace: int, caller: str, callee: str) -> list[Span]:
    """caller SERVER(1) → caller CLIENT(2) → callee SERVER(3)."""
    base = trace * 10
    return [
        _span(ctx, trace, base + 1, None, caller, start_ms=trace),
        _span(ctx, trace, base + 2, base + 1, caller, SpanKind.CLIENT, start_ms=trace + 1),
        _span(ctx, trace, base + 3, base + 2, callee, start_ms=trace + 2, dur_ms=5),
    ]


async def _edges(sessions: Sessions, ctx: TenantContext) -> dict[tuple[str, str, str], int]:
    async with sessions() as s:
        rows = await s.scalars(select(ServiceEdge).where(ServiceEdge.tenant_id == ctx.tenant_id))
        return {(r.environment, r.caller_service, r.callee_service): r.call_count for r in rows}


async def _rollups(sessions: Sessions, ctx: TenantContext) -> dict[str, tuple[int, int, int]]:
    async with sessions() as s:
        rows = await s.scalars(
            select(ServiceMinuteRollup).where(ServiceMinuteRollup.tenant_id == ctx.tenant_id)
        )
        return {r.service: (r.request_count, r.error_count, sum(r.latency_buckets)) for r in rows}


async def test_redelivery_changes_nothing(sessions: Sessions, ctx: TenantContext) -> None:
    spans = [s for t in range(1, 6) for s in _call(ctx, t, "orders", "payment")]
    async with sessions.begin() as s:
        first = await record_spans(s, ctx, spans + spans[:3])  # in-batch duplicates too
    assert (first.received, first.inserted) == (18, 15)
    assert first.edges == {("demo", "orders", "payment"): 5}
    edges, rollups = await _edges(sessions, ctx), await _rollups(sessions, ctx)

    async with sessions.begin() as s:
        again = await record_spans(s, ctx, spans)
    assert (again.inserted, again.edges, again.rollup_rows) == (0, {}, 0)
    assert await _edges(sessions, ctx) == edges == {("demo", "orders", "payment"): 5}
    assert await _rollups(sessions, ctx) == rollups == {"orders": (5, 0, 5), "payment": (5, 0, 5)}


async def test_children_before_parents_count_each_call_once(
    sessions: Sessions, ctx: TenantContext
) -> None:
    spans = [s for t in range(1, 4) for s in _call(ctx, t, "gateway", "orders")]
    children = [s for s in spans if s.service == "orders"]
    parents = [s for s in spans if s.service == "gateway"]
    async with sessions.begin() as s:
        assert (await record_spans(s, ctx, children)).edges == {}
    async with sessions.begin() as s:
        assert (await record_spans(s, ctx, parents)).edges == {("demo", "gateway", "orders"): 3}
    async with sessions.begin() as s:  # everything again: still nothing new
        assert (await record_spans(s, ctx, spans)).edges == {}
    assert await _edges(sessions, ctx) == {("demo", "gateway", "orders"): 3}


async def test_concurrent_parent_and_child_transactions_count_each_call_once(
    sessions: Sessions, ctx: TenantContext
) -> None:
    """Both inserts happen before either transaction derives edges. Without the per-tenant
    lock neither would see the other's uncommitted span and both calls would be lost."""
    spans = [s for t in range(1, 21) for s in _call(ctx, t, "payment", "fraud")]
    children = [s for s in spans if s.service == "fraud"]
    parents = [s for s in spans if s.service == "payment"]
    s1, s2 = sessions(), sessions()
    try:
        await s1.begin()
        await s2.begin()
        new_children = await insert_spans(s1, ctx, children)
        new_parents = await insert_spans(s2, ctx, parents)

        async def fold(session: AsyncSession, new: list[Span]) -> None:
            await fold_new_spans(session, ctx, new)
            await asyncio.sleep(0.2)  # hold the lock a moment: the other one must wait
            await session.commit()

        await asyncio.gather(fold(s1, new_children), fold(s2, new_parents))
    finally:
        await s1.close()
        await s2.close()
    assert await _edges(sessions, ctx) == {("demo", "payment", "fraud"): 20}


async def test_rollups_merge_by_addition(sessions: Sessions, ctx: TenantContext) -> None:
    a = [
        _span(ctx, 1, 1, None, "inventory", dur_ms=18),
        _span(ctx, 2, 2, None, "inventory", dur_ms=400, error=True),
    ]
    b = [_span(ctx, 3, 3, None, "inventory", dur_ms=18, start_ms=30_000)]
    internal = [_span(ctx, 3, 4, 3, "inventory", SpanKind.INTERNAL, dur_ms=5)]
    async with sessions.begin() as s:
        await record_spans(s, ctx, a)
    async with sessions.begin() as s:
        await record_spans(s, ctx, b + internal)
    async with sessions() as s:
        [row] = list(
            await s.scalars(
                select(ServiceMinuteRollup).where(ServiceMinuteRollup.tenant_id == ctx.tenant_id)
            )
        )
    assert row.minute == BASE
    assert (row.request_count, row.error_count) == (3, 1)  # INTERNAL spans are not requests
    assert row.duration_sum_ms == pytest.approx(436.0)
    assert row.duration_max_ms == pytest.approx(400.0)
    expected = [0] * LATENCY_BUCKETS
    for d in (18, 400, 18):
        expected[bucket_index(d)] += 1
    assert row.latency_buckets == expected

    async with sessions.begin() as s:
        assert await delete_rollups_older_than(s, 1, now=BASE + timedelta(days=2)) >= 1
        assert await existing_tenants(s, [ctx.tenant_id, uuid.uuid4()]) == {ctx.tenant_id}


async def test_spans_of_another_tenant_are_refused(sessions: Sessions, ctx: TenantContext) -> None:
    other = TenantContext(tenant_id=uuid.uuid4(), principal=Principal.SYSTEM)
    with pytest.raises(ValueError, match="tenant"):
        async with sessions.begin() as s:
            await record_spans(s, other, [_span(ctx, 1, 1, None, "x")])


async def test_load_analysis_spans_filters_and_samples_whole_traces(
    sessions: Sessions, ctx: TenantContext
) -> None:
    spans = [s for t in range(1, 401) for s in _call(ctx, t, "orders", "payment")]
    spans += [_span(ctx, 9001, 1, None, "orders", env=None)]  # no environment: kept
    spans += [_span(ctx, 9002, 1, None, "orders", env="prod")]  # other environment: dropped
    async with sessions.begin() as s:
        await record_spans(s, ctx, spans)
    window = {"environment": "demo", "start": BASE, "end": BASE + timedelta(seconds=1)}

    async with sessions() as s:
        full = await load_analysis_spans(s, ctx, max_spans=10_000, **window)
    assert (full.matched, len(full.spans), full.sampled) == (1201, 1201, False)
    assert {s.environment for s in full.spans} == {"demo", None}
    assert full.spans == sorted(full.spans, key=lambda x: (x.start_ns, x.trace_id, x.span_id))

    async with sessions() as s:
        a = await load_analysis_spans(s, ctx, max_spans=600, **window)
        b = await load_analysis_spans(s, ctx, max_spans=600, **window)
    assert a.sampled
    assert a.matched == 1201
    assert [x.key for x in a.spans] == [x.key for x in b.spans]  # deterministic
    assert 400 < len(a.spans) < 800  # about half
    per_trace: dict[str, int] = {}
    for x in a.spans:
        per_trace[x.trace_id] = per_trace.get(x.trace_id, 0) + 1
    assert set(per_trace.values()) <= {1, 3}  # traces are kept whole
    meta = a.describe(BASE, BASE)
    assert meta["sampled"]
    assert meta["method"] == "trace_id_md5_32"
    assert meta["sample_fraction"] == pytest.approx(600 / 1201, rel=1e-6)

    other = TenantContext(tenant_id=uuid.uuid4(), principal=Principal.SYSTEM)
    async with sessions() as s:
        assert (await load_analysis_spans(s, other, max_spans=10, **window)).spans == []
    assert sample_threshold(10, 10) is None


async def _deployment(
    sessions: Sessions, ctx: TenantContext, *, env: str, minutes: int
) -> uuid.UUID:
    async with sessions.begin() as s:
        rec = await record_deployment(
            s,
            ctx,
            DeploymentEvent(
                event_id=uuid7(),
                tenant_id=ctx.tenant_id,
                occurred_at=BASE,
                schema_version=2,
                service="payment",
                environment=env,
                version=f"v{minutes}",
                previous_version=None,
                commit_sha="abcdef1",
                deployed_at=BASE + timedelta(minutes=minutes),
                deployer=None,
                ci_run_url=None,
                repository=None,
                pull_request=None,
                risk_assessment_id=None,
                labels=MappingProxyType({}),
            ),
        )
    assert rec.deployment_id is not None
    return rec.deployment_id


def _analysis(dep: uuid.UUID, kind: JobKind, status: str, **kw: Any) -> AnalysisWrite:
    fields: dict[str, Any] = {
        "deployment_id": dep,
        "kind": kind,
        "status": status,
        "severity_score": 50.0,
        "severity_band": "HIGH",
        "probable_origin": "payment",
        "confidence": "HIGH",
        "result": {"blast_radius": {"affected": ["orders", "gateway"]}},
        "engine_version": "regression-engine/1.0.0",
        "windows": {},
        "sampling": {},
    }
    fields.update(kw)
    return AnalysisWrite(**fields)


async def test_analyses_upsert_per_kind_and_regression_listing(
    sessions: Sessions, ctx: TenantContext
) -> None:
    regressed = await _deployment(sessions, ctx, env="staging", minutes=1)
    healthy = await _deployment(sessions, ctx, env="staging", minutes=2)
    async with sessions.begin() as s:
        first = await upsert_analysis(
            s, ctx, _analysis(regressed, JobKind.EARLY_5M, "INSUFFICIENT_DATA")
        )
        again = await upsert_analysis(s, ctx, _analysis(regressed, JobKind.EARLY_5M, "WARNING"))
        await upsert_analysis(s, ctx, _analysis(regressed, JobKind.FINAL, "REGRESSED"))
        # The latest (final) analysis of `healthy` supersedes its early warning.
        await upsert_analysis(s, ctx, _analysis(healthy, JobKind.EARLY_10M, "WARNING"))
        await upsert_analysis(s, ctx, _analysis(healthy, JobKind.FINAL, "HEALTHY"))
    assert first == again  # an upsert keeps the row's id

    async with sessions() as s:
        rows = await list_analyses(s, ctx, regressed)
        assert [r.kind for r in rows] == [JobKind.FINAL, JobKind.EARLY_5M]
        assert rows[1].status == "WARNING"
        listed = await list_regressions(s, ctx, limit=10)
        assert [(r.d_id, r.a_kind, r.a_affected) for r in listed] == [
            (regressed, JobKind.FINAL, ["orders", "gateway"])
        ]
        assert await list_regressions(s, ctx, limit=10, environment="prod") == []
        latest = await latest_environment_analysis(s, ctx, "staging")
        assert latest is not None
        assert (latest.deployment_id, latest.kind) == (
            healthy,
            JobKind.FINAL,
        )

        other = TenantContext(tenant_id=uuid.uuid4(), principal=Principal.SYSTEM)
        assert await list_analyses(s, other, regressed) == []
        assert await list_regressions(s, other, limit=10) == []
        assert await latest_environment_analysis(s, other, "staging") is None
    async with sessions() as s:  # unbound: the calls above bound this transaction to `other`
        count = await s.scalar(
            select(RegressionAnalysisRecord.id).where(
                RegressionAnalysisRecord.deployment_id == regressed,
                RegressionAnalysisRecord.kind == JobKind.EARLY_5M,
            )
        )
        assert count == first


async def test_deployments_carry_their_latest_analysis_and_filter_by_since(
    sessions: Sessions, ctx: TenantContext
) -> None:
    d1 = await _deployment(sessions, ctx, env="demo", minutes=0)
    d2 = await _deployment(sessions, ctx, env="demo", minutes=10)
    d3 = await _deployment(sessions, ctx, env="prod", minutes=20)
    async with sessions.begin() as s:
        await upsert_analysis(s, ctx, _analysis(d1, JobKind.EARLY_5M, "WARNING"))
        await upsert_analysis(
            s, ctx, _analysis(d1, JobKind.FINAL, "REGRESSED", windows={"post_end": "t"})
        )
        await upsert_analysis(s, ctx, _analysis(d2, JobKind.EARLY_10M, "HEALTHY"))
    other = TenantContext(tenant_id=uuid.uuid4(), principal=Principal.SYSTEM)
    async with sessions() as s:
        rows = await list_deployments_with_latest_analysis(s, ctx, limit=10)
        recent = await list_deployments_with_latest_analysis(
            s, ctx, limit=10, since=BASE + timedelta(minutes=10)
        )
        demo = await list_deployments_with_latest_analysis(s, ctx, limit=10, environment="demo")
        one = await get_deployment_with_latest_analysis(s, ctx, d1)
        windows = await latest_analysis_windows(s, ctx, d1)
        assert await list_deployments_with_latest_analysis(s, other, limit=10) == []
        assert await get_deployment_with_latest_analysis(s, other, d1) is None
        assert await latest_analysis_windows(s, other, d1) is None
        assert await latest_analysis_windows(s, ctx, d3) is None
    verdicts = [(d.id, None if a is None else (a.kind, a.status)) for d, a in rows]
    assert verdicts == [
        (d3, None),
        (d2, (JobKind.EARLY_10M, "HEALTHY")),
        (d1, (JobKind.FINAL, "REGRESSED")),  # the most complete kind wins
    ]
    assert [d.id for d, _ in recent] == [d3, d2]  # deployed_at >= since (inclusive)
    assert [d.id for d, _ in demo] == [d2, d1]
    assert one is not None
    assert one[0].id == d1
    assert one[1] is not None
    assert (one[1].severity_band, one[1].probable_origin) == ("HIGH", "payment")
    assert windows is not None
    assert (windows.kind, windows.windows) == (JobKind.FINAL, {"post_end": "t"})


async def test_environments_minute_series_and_overview_counts(
    sessions: Sessions, ctx: TenantContext
) -> None:
    spans = [s for t in range(1, 6) for s in _call(ctx, t, "orders", "payment")]
    spans.append(_span(ctx, 99, 991, None, "nowhere", env=None))  # environment not reported
    spans.append(_span(ctx, 98, 981, None, "orders", start_ms=60_000, dur_ms=50, error=True))
    async with sessions.begin() as s:
        await record_spans(s, ctx, spans)
    old = await _deployment(sessions, ctx, env="prod", minutes=-30)
    new = await _deployment(sessions, ctx, env="prod", minutes=-5)
    async with sessions.begin() as s:
        await upsert_analysis(s, ctx, _analysis(old, JobKind.FINAL, "HEALTHY"))
        await upsert_analysis(s, ctx, _analysis(new, JobKind.FINAL, "REGRESSED"))
    other = TenantContext(tenant_id=uuid.uuid4(), principal=Principal.SYSTEM)

    async with sessions() as s:
        envs = await list_environments(s, ctx)
        assert await list_environments(s, other) == []
        series = await minute_series(
            s,
            ctx,
            environment="demo",
            start=BASE - timedelta(minutes=5),
            end=BASE + timedelta(minutes=5),
        )
        payment = await minute_series(
            s,
            ctx,
            environment="demo",
            start=BASE,
            end=BASE + timedelta(minutes=5),
            service="payment",
        )
        first_minute_only = await minute_series(
            s, ctx, environment="demo", start=BASE, end=BASE + timedelta(minutes=1)
        )
        assert (
            await minute_series(
                s, other, environment="demo", start=BASE, end=BASE + timedelta(minutes=5)
            )
            == []
        )
        counts = await overview_counts(
            s,
            ctx,
            deployments_since=BASE - timedelta(days=1),
            assessments_since=BASE - timedelta(days=1),
            services_since=BASE - timedelta(minutes=1),
        )
        later = await overview_counts(
            s,
            ctx,
            deployments_since=BASE - timedelta(minutes=10),
            assessments_since=BASE,
            services_since=BASE + timedelta(minutes=1),
        )
        theirs = await overview_counts(
            s,
            other,
            deployments_since=BASE - timedelta(days=1),
            assessments_since=BASE - timedelta(days=1),
            services_since=BASE - timedelta(days=1),
        )

    by_name = {e.name: e for e in envs}
    assert list(by_name) == ["demo", "prod"]  # '' (not reported) is left out
    demo, prod = by_name["demo"], by_name["prod"]
    assert demo.last_deployment_at is None
    assert demo.last_telemetry_at == BASE + timedelta(minutes=1)  # the later orders minute
    assert demo.last_seen == demo.last_telemetry_at
    assert (prod.last_deployment_at, prod.last_telemetry_at) == (
        BASE - timedelta(minutes=5),
        None,
    )

    assert [(r.service, r.minute, r.requests, r.errors) for r in series] == [
        ("orders", BASE, 5, 0),
        ("orders", BASE + timedelta(minutes=1), 1, 1),
        ("payment", BASE, 5, 0),
    ]
    assert sum(series[0].buckets) == 5
    assert [r.service for r in payment] == ["payment"]
    assert [(r.service, r.minute) for r in first_minute_only] == [
        ("orders", BASE),
        ("payment", BASE),
    ]

    assert (counts.deployments, counts.regressions, counts.active_regressions) == (2, 1, 1)
    assert counts.services_seen == 3  # orders, payment and the environment-less "nowhere"
    assert counts.risk_assessments_by_band == {}
    assert (later.deployments, later.regressions, later.services_seen) == (1, 1, 1)
    assert (theirs.deployments, theirs.active_regressions, theirs.services_seen) == (0, 0, 0)

    # A newer deployment of the same service and environment supersedes the regression.
    fixed = await _deployment(sessions, ctx, env="prod", minutes=0)
    async with sessions.begin() as s:
        await upsert_analysis(s, ctx, _analysis(fixed, JobKind.FINAL, "HEALTHY"))
    async with sessions() as s:
        after = await overview_counts(
            s,
            ctx,
            deployments_since=BASE - timedelta(days=1),
            assessments_since=BASE,
            services_since=BASE,
        )
    assert (after.deployments, after.regressions, after.active_regressions) == (3, 1, 0)
