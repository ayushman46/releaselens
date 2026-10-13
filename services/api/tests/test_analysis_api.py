"""Read APIs over analyses and telemetry aggregates, seeded directly in Postgres."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.apikeys import create_api_key
from rl_api.app import create_app
from rl_api.settings import Settings
from rl_core.events import DeploymentEvent
from rl_core.ids import uuid7
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind, StatusCode, datetime_to_ns
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.admin.partitions import day_of, ensure_partitions_for_days
from rl_db.models import JobKind
from rl_db.repositories import AnalysisWrite, record_deployment, record_spans, upsert_analysis

pytestmark = pytest.mark.integration

T = TypeVar("T")
Sessions = async_sessionmaker[AsyncSession]


def _db[T](url: str, fn: Callable[[Sessions], Awaitable[T]]) -> T:
    async def go() -> T:
        engine = create_engine(url, pool_size=2)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


class Seeded:
    def __init__(
        self, tenant: uuid.UUID, key: str, deployments: dict[str, uuid.UUID], now: datetime
    ) -> None:
        self.tenant, self.key, self.deployments, self.now = tenant, key, deployments, now

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}


def _result(origin: str | None) -> dict[str, Any]:
    return {
        "deployment": {"service": "payment"},
        "services": [
            {"service": "gateway", "status": "REGRESSED"},
            {"service": "orders", "status": "REGRESSED"},
            {"service": "payment", "status": "REGRESSED"},
            {"service": "fraud", "status": "HEALTHY"},
        ],
        "blast_radius": {
            "origin": origin,
            "nodes": [
                {"service": "orders", "impact": "AFFECTED_UPSTREAM"},
                {"service": "gateway", "impact": "AFFECTED_UPSTREAM"},
                {"service": "fraud", "impact": "HEALTHY_DOWNSTREAM"},
            ],
            "affected": ["orders", "gateway"],
        },
    }


def _seed(url: str, *, env: str = "staging") -> Seeded:
    now = datetime.now(UTC)

    async def go(sessions: Sessions) -> Seeded:
        async with sessions.begin() as s:
            org = await create_organization(s, name="A", slug=f"a-{uuid.uuid4().hex[:12]}")
            key = await create_api_key(s, org.id, "ui", ["read"])
            await ensure_partitions_for_days(s, {day_of(now - timedelta(minutes=10)), day_of(now)})
        ctx = TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)
        deployments: dict[str, uuid.UUID] = {}
        async with sessions.begin() as s:
            for name, minutes in (("regressed", 30), ("healthy", 20), ("pending", 10)):
                rec = await record_deployment(
                    s,
                    ctx,
                    DeploymentEvent(
                        event_id=uuid7(),
                        tenant_id=org.id,
                        occurred_at=now,
                        schema_version=2,
                        service="payment",
                        environment=env,
                        version=f"v-{name}",
                        previous_version="v0",
                        commit_sha="abcdef1",
                        deployed_at=now - timedelta(minutes=minutes),
                        deployer=None,
                        ci_run_url=None,
                        repository=None,
                        pull_request=None,
                        risk_assessment_id=None,
                        labels=MappingProxyType({}),
                    ),
                )
                assert rec.deployment_id is not None
                deployments[name] = rec.deployment_id
            base = AnalysisWrite(
                deployment_id=deployments["regressed"],
                kind=JobKind.FINAL,
                status="REGRESSED",
                severity_score=58.6,
                severity_band="HIGH",
                probable_origin="payment",
                confidence="HIGH",
                result=_result("payment"),
                engine_version="regression-engine/1.0.0",
                windows={"post_end": "x"},
                sampling={"sampled": False},
            )
            await upsert_analysis(s, ctx, base)
            await upsert_analysis(
                s, ctx, replace(base, kind=JobKind.EARLY_5M, status="INSUFFICIENT_DATA")
            )
            await upsert_analysis(
                s,
                ctx,
                replace(
                    base,
                    deployment_id=deployments["healthy"],
                    status="HEALTHY",
                    probable_origin=None,
                    confidence=None,
                    severity_score=0.0,
                    severity_band="HEALTHY",
                    result={"services": [{"service": "payment", "status": "HEALTHY"}]},
                ),
            )
        # Telemetry: 10 requests gateway -> orders five minutes ago.
        start = datetime_to_ns(now.replace(second=0, microsecond=0) - timedelta(minutes=5))
        spans: list[Span] = []
        for i in range(10):
            t = f"{uuid.uuid4().int:032x}"
            for sid, parent, service, kind, dur in (
                (1, None, "gateway", SpanKind.SERVER, 40.0),
                (2, 1, "gateway", SpanKind.CLIENT, 35.0),
                (3, 2, "orders", SpanKind.SERVER, 30.0 + i),
            ):
                spans.append(
                    Span(
                        tenant_id=str(org.id),
                        trace_id=t,
                        span_id=f"{sid:016x}",
                        parent_span_id=None if parent is None else f"{parent:016x}",
                        service=service,
                        service_version="v7" if service == "orders" else "v1",
                        environment=env,
                        name=service,
                        kind=kind,
                        start_ns=start + i * 1000 * NS_PER_MS,
                        end_ns=start + i * 1000 * NS_PER_MS + round(dur * NS_PER_MS),
                        status_code=StatusCode.ERROR
                        if (service, i) == ("orders", 0)
                        else StatusCode.UNSET,
                    )
                )
        async with sessions.begin() as s:
            await record_spans(s, ctx, spans)
        return Seeded(org.id, key.plaintext, deployments, now)

    return _db(url, go)


@pytest.fixture(scope="module")
def client(api_db_url: str) -> Iterator[TestClient]:
    settings = Settings(
        env="test", database_url=api_db_url, kafka_bootstrap="127.0.0.1:1", otel_enabled=False
    )
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture(scope="module")
def a(api_db_url: str) -> Seeded:
    return _seed(api_db_url)


@pytest.fixture(scope="module")
def b(api_db_url: str) -> Seeded:
    return _seed(api_db_url)


def test_deployment_analysis_latest_is_the_most_complete_kind(
    client: TestClient, a: Seeded
) -> None:
    dep = a.deployments["regressed"]
    body = client.get(f"/api/v1/deployments/{dep}/analysis", headers=a.auth).json()
    assert body["deployment_id"] == str(dep)
    assert [x["kind"] for x in body["analyses"]] == ["final", "early_5m"]
    latest = body["latest"]
    assert (latest["kind"], latest["status"], latest["probable_origin"]) == (
        "final",
        "REGRESSED",
        "payment",
    )
    assert latest["result"]["blast_radius"]["affected"] == ["orders", "gateway"]

    pending = client.get(
        f"/api/v1/deployments/{a.deployments['pending']}/analysis", headers=a.auth
    ).json()
    assert pending == {
        "deployment_id": str(a.deployments["pending"]),
        "latest": None,
        "analyses": [],
    }


def test_cross_tenant_reads_are_404_like_missing(client: TestClient, a: Seeded, b: Seeded) -> None:
    other = client.get(f"/api/v1/deployments/{a.deployments['regressed']}/analysis", headers=b.auth)
    missing = client.get(f"/api/v1/deployments/{uuid.uuid4()}/analysis", headers=b.auth)
    assert other.status_code == missing.status_code == 404
    assert other.json()["error"]["message"] == missing.json()["error"]["message"]
    ids = {
        i["deployment_id"]
        for i in client.get("/api/v1/regressions", headers=b.auth).json()["items"]
    }
    assert str(a.deployments["regressed"]) not in ids


def test_regressions_lists_only_regressed_or_warning(client: TestClient, a: Seeded) -> None:
    body = client.get("/api/v1/regressions", headers=a.auth).json()
    [item] = body["items"]
    assert item["deployment_id"] == str(a.deployments["regressed"])
    assert (item["analysis_kind"], item["status"], item["severity_band"]) == (
        "final",
        "REGRESSED",
        "HIGH",
    )
    assert (item["probable_origin"], item["confidence"]) == ("payment", "HIGH")
    assert item["affected_services"] == ["orders", "gateway"]
    assert body["next_cursor"] is None
    assert (
        client.get("/api/v1/regressions", params={"environment": "prod"}, headers=a.auth).json()[
            "items"
        ]
        == []
    )
    bad = client.get("/api/v1/regressions", params={"cursor": "%%"}, headers=a.auth)
    assert bad.status_code == 400


def test_services_catalog_rates_and_approximate_p95(client: TestClient, a: Seeded) -> None:
    body = client.get("/api/v1/services", params={"environment": "staging"}, headers=a.auth).json()
    items = {i["service"]: i for i in body["items"]}
    assert set(items) == {"gateway", "orders", "payment"}
    orders = items["orders"]
    assert orders["latest_version_seen"] == "v7"
    h = orders["health"]
    assert (h["requests"], h["errors"]) == (10, 1)
    assert h["request_rate_per_min"] == pytest.approx(10 / 15)
    assert h["error_rate"] == pytest.approx(0.1)
    assert h["latency_approximate"] is True
    low, high = h["latency_p95_bucket_ms"]
    assert low <= 39.0 <= high  # exact p95 of 30..39 ms lies in the reported bucket
    assert low <= h["latency_p95_ms_approx"] <= high
    payment = items["payment"]  # deployed, but no telemetry
    assert payment["last_deployment"]["version"] == "v-pending"
    assert payment["health"]["requests"] == 0
    assert payment["health"]["latency_p95_ms_approx"] is None

    page1 = client.get("/api/v1/services", params={"limit": 2}, headers=a.auth).json()
    page2 = client.get(
        "/api/v1/services", params={"limit": 2, "cursor": page1["next_cursor"]}, headers=a.auth
    ).json()
    names = [i["service"] for i in page1["items"] + page2["items"]]
    assert names == ["gateway", "orders", "payment"]
    assert page2["next_cursor"] is None


def test_service_map_has_edges_and_node_health(client: TestClient, a: Seeded) -> None:
    body = client.get(
        "/api/v1/service-map", params={"environment": "staging"}, headers=a.auth
    ).json()
    assert body["edges"] == [
        {
            "source": "gateway",
            "target": "orders",
            "call_count": 10,
            "first_seen": body["edges"][0]["first_seen"],
            "last_seen": body["edges"][0]["last_seen"],
        }
    ]
    nodes = {n["service"]: n for n in body["nodes"]}
    assert set(nodes) == {"gateway", "orders"}
    # The newest analysed deployment in staging is "healthy": its analysis drives node health.
    assert body["analysis"]["deployment_id"] == str(a.deployments["healthy"])
    assert nodes["orders"]["status"] is None  # not covered by that analysis
    assert not any(n["probable_origin"] for n in nodes.values())
    assert client.get("/api/v1/service-map", headers=a.auth).status_code == 422


# ----------------------------------------------------------------------------- deployment verdicts
def _analyse(url: str, seeded: Seeded, name: str, **fields: Any) -> None:
    values: dict[str, Any] = {
        "deployment_id": seeded.deployments[name],
        "kind": JobKind.FINAL,
        "status": "REGRESSED",
        "severity_score": 71.5,
        "severity_band": "CRITICAL",
        "probable_origin": "payment",
        "confidence": "MEDIUM",
        "result": _result("payment"),
        "engine_version": "regression-engine/1.0.0",
        "windows": {},
        "sampling": {},
    }
    values.update(fields)

    async def go(sessions: Sessions) -> None:
        ctx = TenantContext(tenant_id=seeded.tenant, principal=Principal.SYSTEM)
        async with sessions.begin() as s:
            await upsert_analysis(s, ctx, AnalysisWrite(**values))

    _db(url, go)


def test_deployment_rows_carry_the_latest_verdict_from_one_query(
    client: TestClient, a: Seeded, b: Seeded
) -> None:
    statements: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = client.app.state.db_engine.sync_engine  # type: ignore[attr-defined]
    event.listen(engine, "before_cursor_execute", record)
    try:
        body = client.get("/api/v1/deployments", headers=a.auth).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    by_id = {i["id"]: i for i in body["items"]}
    assert [i["version"] for i in body["items"]] == ["v-pending", "v-healthy", "v-regressed"]
    assert by_id[str(a.deployments["pending"])]["latest_analysis"] is None
    regressed = by_id[str(a.deployments["regressed"])]["latest_analysis"]
    assert {k: regressed[k] for k in regressed if k not in ("id", "analysed_at")} == {
        "kind": "final",  # the most complete kind, not early_5m
        "status": "REGRESSED",
        "severity_score": 58.6,
        "severity_band": "HIGH",
        "probable_origin": "payment",
        "confidence": "HIGH",
    }
    healthy = by_id[str(a.deployments["healthy"])]["latest_analysis"]
    assert (healthy["status"], healthy["probable_origin"]) == ("HEALTHY", None)
    # No query per row: the verdicts come with the deployments in one statement.
    touching = [s for s in statements if "regression_analyses" in s]
    assert len(touching) == 1
    assert "deployments" in touching[0]

    one = client.get(f"/api/v1/deployments/{a.deployments['regressed']}", headers=a.auth).json()
    assert one["latest_analysis"]["kind"] == "final"
    assert one["latest_analysis"]["severity_score"] == 58.6
    assert len(one["analysis_jobs"]) == 3
    # Another tenant's deployment stays a 404, verdict or not.
    cross = client.get(f"/api/v1/deployments/{a.deployments['regressed']}", headers=b.auth)
    assert cross.status_code == 404
    assert cross.json()["error"]["code"] == "not_found"


def test_since_filters_deployments_and_regressions(client: TestClient, a: Seeded) -> None:
    def ids(path: str, since: datetime) -> list[str]:
        resp = client.get(path, params={"since": since.isoformat()}, headers=a.auth)
        assert resp.status_code == 200, resp.text
        return [i.get("deployment_id", i.get("id")) for i in resp.json()["items"]]

    recent = a.now - timedelta(minutes=25)
    assert ids("/api/v1/deployments", recent) == [
        str(a.deployments["pending"]),
        str(a.deployments["healthy"]),
    ]
    assert ids("/api/v1/deployments", a.now + timedelta(minutes=1)) == []
    # deployed_at >= since is inclusive
    assert str(a.deployments["regressed"]) in ids(
        "/api/v1/deployments", a.now - timedelta(minutes=30)
    )
    assert ids("/api/v1/regressions", recent) == []
    assert ids("/api/v1/regressions", a.now - timedelta(hours=1)) == [
        str(a.deployments["regressed"])
    ]
    # An offset other than UTC is the same instant.
    plus2 = (a.now - timedelta(minutes=25)).astimezone(timezone(timedelta(hours=2)))
    assert len(ids("/api/v1/deployments", plus2)) == 2
    for path in ("/api/v1/deployments", "/api/v1/regressions"):
        naive = client.get(path, params={"since": "2026-01-01T00:00:00"}, headers=a.auth)
        assert naive.status_code == 422, path
        garbage = client.get(path, params={"since": "yesterday"}, headers=a.auth)
        assert garbage.status_code == 422, path


# ----------------------------------------------------------------------------- timeseries
def _minutes(body: dict[str, Any]) -> int:
    start = datetime.fromisoformat(body["start"])
    end = datetime.fromisoformat(body["end"])
    return int((end - start) / timedelta(minutes=1))


def test_timeseries_with_default_windows_before_any_analysis(client: TestClient, a: Seeded) -> None:
    dep = a.deployments["pending"]  # no analysis yet: spec default windows
    body = client.get(f"/api/v1/deployments/{dep}/timeseries", headers=a.auth).json()
    w = body["windows"]
    assert (w["source"], w["analysis_id"], w["analysis_kind"]) == ("default", None, None)
    deployed = datetime.fromisoformat(w["deployed_at"])
    assert datetime.fromisoformat(w["baseline_start"]) == deployed - timedelta(minutes=30)
    assert datetime.fromisoformat(w["post_end"]) == deployed + timedelta(minutes=15)
    assert body["environment"] == "staging"
    assert body["service"] is None
    assert (body["step_seconds"], body["latency_approximate"]) == (60, True)
    start = datetime.fromisoformat(body["start"])
    assert start == datetime.fromisoformat(w["baseline_start"]).replace(second=0, microsecond=0)
    # The post window has not closed: the series stops before the still-filling minute.
    assert datetime.fromisoformat(body["end"]) <= datetime.now(UTC)

    series = {s["service"]: s["points"] for s in body["series"]}
    assert set(series) == {"gateway", "orders"}  # SERVER spans in staging; payment sent none
    n = _minutes(body)
    for points in series.values():
        assert len(points) == n
        assert [datetime.fromisoformat(p["minute"]) for p in points] == [
            start + timedelta(minutes=i) for i in range(n)
        ]
    busy = [p for p in series["orders"] if p["requests"]]
    # Exactly this tenant's 10 requests (tenant b has an identical copy that must not leak in).
    assert [(p["requests"], p["errors"]) for p in busy] == [(10, 1)]
    point = busy[0]
    assert point["error_rate"] == pytest.approx(0.1)
    assert point["latency_mean_ms"] == pytest.approx(34.5)
    assert 32.0 <= point["latency_p50_ms_approx"] <= 45.26  # exact p50 34.5 ms, bucket (32, 45.25]
    assert 32.0 <= point["latency_p95_ms_approx"] <= 45.26  # exact p95 ≈ 39 ms
    idle = next(p for p in series["orders"] if not p["requests"])
    assert idle == {
        "minute": idle["minute"],
        "requests": 0,
        "errors": 0,
        "error_rate": None,
        "latency_mean_ms": None,
        "latency_p50_ms_approx": None,
        "latency_p95_ms_approx": None,
    }


def test_timeseries_uses_the_latest_analysis_windows_and_filters_by_service(
    client: TestClient, api_db_url: str
) -> None:
    c = _seed(api_db_url)
    deployed = c.now - timedelta(minutes=10)
    windows = {
        "profile": "demo",
        "baseline_minutes": 3,
        "warmup_minutes": 1,
        "post_end_minutes": 8,
        "deployed_at": deployed.isoformat(),
        "baseline_start": (deployed - timedelta(minutes=3)).isoformat(),
        "baseline_end": deployed.isoformat(),
        "post_start": (deployed + timedelta(minutes=1)).isoformat(),
        "post_end": (deployed + timedelta(minutes=8)).isoformat(),
        "effective_post_end": (deployed + timedelta(minutes=8)).isoformat(),
        "as_of": None,
    }
    _analyse(api_db_url, c, "pending", windows=windows, kind=JobKind.EARLY_5M)
    dep = c.deployments["pending"]
    auth = {"Authorization": f"Bearer {c.key}"}
    body = client.get(f"/api/v1/deployments/{dep}/timeseries", headers=auth).json()
    w = body["windows"]
    assert (w["source"], w["analysis_kind"]) == ("analysis", "early_5m")
    assert datetime.fromisoformat(body["start"]) == (deployed - timedelta(minutes=3)).replace(
        second=0, microsecond=0
    )
    assert datetime.fromisoformat(body["end"]) == (deployed + timedelta(minutes=9)).replace(
        second=0, microsecond=0
    )
    assert _minutes(body) == 12

    only = client.get(
        f"/api/v1/deployments/{dep}/timeseries", params={"service": "orders"}, headers=auth
    ).json()
    assert [s["service"] for s in only["series"]] == ["orders"]
    assert sum(p["requests"] for p in only["series"][0]["points"]) == 10
    unknown = client.get(
        f"/api/v1/deployments/{dep}/timeseries", params={"service": "nope"}, headers=auth
    ).json()
    [empty] = unknown["series"]
    assert empty["service"] == "nope"
    assert len(empty["points"]) == 12
    assert all(p["requests"] == 0 for p in empty["points"])


def test_timeseries_falls_back_to_default_windows_when_stored_ones_are_unusable(
    client: TestClient, a: Seeded
) -> None:
    # The seeded analyses store windows={"post_end": "x"}.
    dep = a.deployments["regressed"]
    body = client.get(f"/api/v1/deployments/{dep}/timeseries", headers=a.auth).json()
    assert body["windows"]["source"] == "default"


def test_timeseries_is_tenant_isolated(client: TestClient, a: Seeded, b: Seeded) -> None:
    other = client.get(f"/api/v1/deployments/{a.deployments['pending']}/timeseries", headers=b.auth)
    missing = client.get(f"/api/v1/deployments/{uuid.uuid4()}/timeseries", headers=b.auth)
    assert other.status_code == missing.status_code == 404
    assert other.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert client.get(f"/api/v1/deployments/{uuid.uuid4()}/timeseries").status_code == 401


# ----------------------------------------------------------------------------- overview
def test_environments_are_per_tenant_with_last_seen(
    client: TestClient, a: Seeded, api_db_url: str
) -> None:
    prod = _seed(api_db_url, env="prod")
    mine = client.get("/api/v1/environments", headers=a.auth).json()["items"]
    theirs = client.get(
        "/api/v1/environments", headers={"Authorization": f"Bearer {prod.key}"}
    ).json()["items"]
    assert [e["name"] for e in mine] == ["staging"]
    assert [e["name"] for e in theirs] == ["prod"]
    env = mine[0]
    last_deployment = datetime.fromisoformat(env["last_deployment_at"])
    assert last_deployment == a.now - timedelta(minutes=10)  # "pending"
    telemetry = datetime.fromisoformat(env["last_telemetry_at"])
    # the newest gateway → orders call started 5 minutes ago + 9 s
    assert a.now - timedelta(minutes=6) <= telemetry <= a.now - timedelta(minutes=4)
    assert datetime.fromisoformat(env["last_seen"]) == max(last_deployment, telemetry)


def test_overview_stats_are_exact_and_per_tenant(client: TestClient, api_db_url: str) -> None:
    c = _seed(api_db_url)
    auth = {"Authorization": f"Bearer {c.key}"}
    before = client.get("/api/v1/stats/overview", headers=auth).json()
    assert before["deployments_7d"] == 3
    assert before["regressions_7d"] == 1  # "regressed"
    # The newest deployment of payment/staging ("pending") has no analysis: nothing active.
    assert before["active_regressions"] == 0
    assert before["services_seen_24h"] == 2  # gateway and orders sent requests
    assert before["risk_assessments_7d"] == {
        "LOW": 0,
        "MEDIUM": 0,
        "HIGH": 0,
        "CRITICAL": 0,
        "total": 0,
    }
    as_of = datetime.fromisoformat(before["as_of"])
    assert datetime.fromisoformat(before["deployments_since"]) == as_of - timedelta(days=7)
    assert datetime.fromisoformat(before["services_since"]) == as_of - timedelta(hours=24)

    _analyse(api_db_url, c, "pending", status="WARNING")
    after = client.get("/api/v1/stats/overview", headers=auth).json()
    assert (after["regressions_7d"], after["active_regressions"]) == (2, 1)
    regressions = client.get(
        "/api/v1/regressions", params={"since": after["deployments_since"]}, headers=auth
    ).json()
    assert len(regressions["items"]) == after["regressions_7d"]

    # Another tenant's numbers are its own.
    other = _seed(api_db_url, env="prod")
    theirs = client.get(
        "/api/v1/stats/overview", headers={"Authorization": f"Bearer {other.key}"}
    ).json()
    assert (theirs["deployments_7d"], theirs["active_regressions"]) == (3, 0)
    assert client.get("/api/v1/stats/overview").status_code == 401
