"""Read APIs over analyses and telemetry aggregates, seeded directly in Postgres."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
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
    def __init__(self, tenant: uuid.UUID, key: str, deployments: dict[str, uuid.UUID]) -> None:
        self.tenant, self.key, self.deployments = tenant, key, deployments

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
        return Seeded(org.id, key.plaintext, deployments)

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
