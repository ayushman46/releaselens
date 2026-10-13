"""End to end: OTLP → API → Kafka → span consumer → Postgres → analysis job → outbox → read API.

Scenario 1 (payment v2.17 → v2.18 adds +250 ms and +3.5 % errors to payment's own work) and the
control (payment v1 → v1) from the regression engine's trace generator go through the real path:

    POST /v1/traces (protobuf, gzip) → telemetry.spans → SpanConsumer → spans/edges/rollups
    POST /api/v1/deployments → deployment.events → DeploymentConsumer → deployments + jobs
    JobRunner (early_5m, early_10m, final) → regression_analyses + analysis.results outbox rows
    GET /api/v1/deployments/{id}/analysis, /regressions, /service-map, /services

The final verdict read back from Postgres must equal the engine's verdict on the generated spans
in memory: storage and reload may not change any evidence.

This module uses a database of its own (the job queue spans tenants by design).
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
import tracegen as tg
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.apikeys import create_api_key
from rl_api.app import create_app
from rl_api.settings import Settings as ApiSettings
from rl_api.testing import protobuf_body
from rl_core.events import ANALYSIS_RESULTS, DEPLOYMENT_EVENTS, TELEMETRY_SPANS, DeploymentEvent
from rl_core.events.analysis import analysis_key, parse_analysis
from rl_core.regression.analysis import analyse_deployment
from rl_core.regression.evidence import DeploymentRef
from rl_core.telemetry.model import Span
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.admin.jobs import ClaimedJob
from rl_db.models import Deployment, JobKind, JobStatus, OutboxEvent, RegressionAnalysisRecord
from rl_db.models.jobs import AnalysisJob
from rl_db.repositories import record_deployment
from rl_worker.analysis import AnalysisJobHandler, AnalysisNotDueError
from rl_worker.consumers.deployments import DeploymentConsumer
from rl_worker.consumers.spans import SpanConsumer
from rl_worker.jobs import JobRunner, default_registry
from rl_worker.kafka import AsyncProducer
from rl_worker.retry import Backoff
from rl_worker.testing import wait_until

pytestmark = [pytest.mark.integration, pytest.mark.slow]

Sessions = async_sessionmaker[AsyncSession]
FAST = Backoff(base_s=0.05, cap_s=0.5, max_attempts=3)
CHUNK = 1500  # spans per OTLP request


def measure(name: str, value: float, unit: str = "s") -> None:
    print(f"[measure] {name}={value:.3f}{unit}", file=sys.stderr)


@pytest.fixture(scope="module")
def e2e_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_e2e")


@pytest.fixture
async def db(e2e_db_url: str) -> Any:
    engine = create_engine(e2e_db_url, pool_size=6)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture(scope="module")
def api(e2e_db_url: str, kafka_bootstrap: str) -> Iterator[TestClient]:
    settings = ApiSettings(
        env="test", database_url=e2e_db_url, kafka_bootstrap=kafka_bootstrap, otel_enabled=False
    )
    with TestClient(create_app(settings)) as client:
        yield client


@dataclass
class Tenant:
    id: uuid.UUID
    key: str

    @property
    def auth(self) -> dict[str, str]:
        return {"x-releaselens-key": self.key}


async def _tenant(db: Sessions, scopes: list[str]) -> Tenant:
    async with db.begin() as s:
        org = await create_organization(s, name="E2E", slug=f"e2e-{uuid.uuid4().hex[:12]}")
        key = await create_api_key(s, org.id, "e2e", scopes)
    return Tenant(org.id, key.plaintext)


ALL_SCOPES = ["telemetry:write", "deployments:write", "read"]


def _push(api: TestClient, tenant: Tenant, spans: list[Span]) -> None:
    for i in range(0, len(spans), CHUNK):
        resp = api.post(
            "/v1/traces",
            content=protobuf_body(spans[i : i + CHUNK], compress=True),
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "gzip",
                **tenant.auth,
            },
        )
        assert resp.status_code == 200, resp.text
        assert not ExportTraceServiceResponse.FromString(resp.content).HasField("partial_success")


async def _consume_spans(
    bootstrap: str, db: Sessions, producer: AsyncProducer, expect_new: int
) -> SpanConsumer:
    consumer = SpanConsumer(
        bootstrap_servers=bootstrap,
        sessions=db,
        producer=producer,
        client_id="e2e-spans",
        group_id=f"e2e-spans-{uuid.uuid4().hex[:8]}",
        topic=TELEMETRY_SPANS,
        backoff=FAST,
        poll_timeout_s=0.2,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        await wait_until(lambda: consumer.stats.spans_inserted >= expect_new, timeout_s=120)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)
    return consumer


async def _record_deployment(
    api: TestClient,
    tenant: Tenant,
    bootstrap: str,
    db: Sessions,
    producer: AsyncProducer,
    body: dict[str, Any],
) -> uuid.UUID:
    resp = api.post("/api/v1/deployments", json=body, headers=tenant.auth)
    assert resp.status_code == 202, resp.text
    event_id = uuid.UUID(resp.json()["event_id"])
    consumer = DeploymentConsumer(
        bootstrap_servers=bootstrap,
        sessions=db,
        producer=producer,
        client_id="e2e-deployments",
        group_id=f"e2e-deployments-{uuid.uuid4().hex[:8]}",
        topic=DEPLOYMENT_EVENTS,
        backoff=FAST,
        poll_timeout_s=0.2,
    )

    async def recorded() -> uuid.UUID | None:
        async with db() as s:
            return await s.scalar(select(Deployment.id).where(Deployment.event_id == event_id))

    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        await wait_until(recorded, timeout_s=60)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)
    deployment_id = await recorded()
    assert deployment_id is not None
    return deployment_id


async def _run_scenario(
    scenario: tg.Scenario,
    *,
    tenant: Tenant,
    environment: str,
    api: TestClient,
    db: Sessions,
    producer: AsyncProducer,
    bootstrap: str,
) -> tuple[uuid.UUID, list[Span], datetime]:
    deployed_at = (datetime.now(UTC) - timedelta(minutes=16)).replace(microsecond=0)
    spans = tg.generate(
        replace(scenario, tenant_id=str(tenant.id), environment=environment), 0, deployed_at
    )
    started = time.perf_counter()
    _push(api, tenant, spans)
    measure(f"{scenario.name}.ingest_{len(spans)}_spans_via_api", time.perf_counter() - started)
    started = time.perf_counter()
    consumer = await _consume_spans(bootstrap, db, producer, len(spans))
    measure(f"{scenario.name}.consume_to_postgres", time.perf_counter() - started)
    assert consumer.stats.spans_inserted == len(spans)

    deployment_id = await _record_deployment(
        api,
        tenant,
        bootstrap,
        db,
        producer,
        {
            "service": scenario.deployed_service,
            "environment": environment,
            "version": scenario.version,
            "previous_version": scenario.previous_version,
            "commit_sha": "0123456789abcdef0123456789abcdef01234567",
            "deployed_at": deployed_at.isoformat(),
        },
    )
    runner = JobRunner(db, default_registry(AnalysisJobHandler(db)), worker_id="e2e")
    started = time.perf_counter()
    assert await runner.run_once() == 3  # early_5m, early_10m and final are all due
    measure(f"{scenario.name}.three_analysis_jobs", time.perf_counter() - started)
    assert runner.stats.succeeded == 3, vars(runner.stats)
    return deployment_id, spans, deployed_at


async def _rows(db: Sessions, deployment_id: uuid.UUID) -> dict[JobKind, RegressionAnalysisRecord]:
    async with db() as s:
        rows = await s.scalars(
            select(RegressionAnalysisRecord).where(
                RegressionAnalysisRecord.deployment_id == deployment_id
            )
        )
        return {r.kind: r for r in rows}


def _verdict(result: dict[str, Any]) -> dict[str, Any]:
    triage = result["triage"] or {}
    radius = result["blast_radius"] or {}
    return {
        "status": result["status"],
        "severity": result["severity"],
        "services": {s["service"]: s["status"] for s in result["services"]},
        "probable_origin": triage.get("probable_origin"),
        "confidence": triage.get("confidence"),
        "candidates": [(c["service"], c["score"]) for c in triage.get("candidates", [])],
        "affected": radius.get("affected"),
    }


async def test_payment_regression_end_to_end(
    api: TestClient, db: Sessions, producer: AsyncProducer, kafka_bootstrap: str
) -> None:
    tenant = await _tenant(db, ALL_SCOPES)
    other = await _tenant(db, ["read"])
    scenario = tg.payment_regression()
    deployment_id, spans, deployed_at = await _run_scenario(
        scenario,
        tenant=tenant,
        environment="staging",
        api=api,
        db=db,
        producer=producer,
        bootstrap=kafka_bootstrap,
    )

    rows = await _rows(db, deployment_id)
    assert set(rows) == set(JobKind)
    final = rows[JobKind.FINAL]
    assert (final.status, final.probable_origin, final.confidence) == (
        "REGRESSED",
        "payment",
        "HIGH",
    )
    assert final.severity_band in {"HIGH", "CRITICAL"}
    assert final.result["blast_radius"]["affected"] == ["orders", "gateway"]
    assert final.windows["profile"] == "default"
    assert final.windows["as_of"] is None
    assert final.sampling["sampled"] is False
    # Early kinds analyse "as of" their scheduled time, not the (late) run time.
    for kind, minutes in ((JobKind.EARLY_5M, 5), (JobKind.EARLY_10M, 10)):
        as_of = datetime.fromisoformat(rows[kind].windows["as_of"])
        assert as_of == deployed_at + timedelta(minutes=minutes)

    # Storage and reload change nothing: the same verdict as the engine on the spans in memory.
    direct = analyse_deployment(
        spans,
        DeploymentRef(
            service="payment",
            environment="staging",
            version="v2.18",
            previous_version="v2.17",
            deployed_at=deployed_at,
            tenant_id=str(tenant.id),
        ),
    )
    assert _verdict(final.result) == _verdict(direct.model_dump(mode="json"))

    # One analysis.results outbox message per kind, in the shared contract.
    async with db() as s:
        outbox = list(
            await s.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.tenant_id == tenant.id, OutboxEvent.topic == ANALYSIS_RESULTS
                )
            )
        )
        jobs = list(
            await s.scalars(select(AnalysisJob).where(AnalysisJob.deployment_id == deployment_id))
        )
    assert all(j.status is JobStatus.SUCCEEDED for j in jobs)
    assert len(outbox) == 3
    assert {o.key for o in outbox} == {analysis_key(tenant.id, deployment_id)}
    envelopes = {e.payload.kind: e for e in (parse_analysis(_json(o.payload)) for o in outbox)}
    summary = envelopes["final"].payload
    assert envelopes["final"].tenant_id == tenant.id
    assert (summary.status, summary.probable_origin, summary.confidence) == (
        "REGRESSED",
        "payment",
        "HIGH",
    )
    assert summary.affected_services == ["orders", "gateway"]
    assert summary.analysis_id == final.id

    # Read APIs.
    body = api.get(f"/api/v1/deployments/{deployment_id}/analysis", headers=tenant.auth).json()
    assert body["latest"]["kind"] == "final"
    assert body["latest"]["probable_origin"] == "payment"
    assert [a["kind"] for a in body["analyses"]] == ["final", "early_10m", "early_5m"]
    denied = api.get(f"/api/v1/deployments/{deployment_id}/analysis", headers=other.auth)
    assert denied.status_code == 404
    regressions = api.get("/api/v1/regressions", headers=tenant.auth).json()["items"]
    assert [r["deployment_id"] for r in regressions] == [str(deployment_id)]
    assert api.get("/api/v1/regressions", headers=other.auth).json()["items"] == []

    graph = api.get(
        "/api/v1/service-map", params={"environment": "staging"}, headers=tenant.auth
    ).json()
    edges = {(e["source"], e["target"]): e["call_count"] for e in graph["edges"]}
    by_key = {s.key: s for s in spans}
    exact: dict[tuple[str, str], int] = {}
    for s in spans:
        parent = by_key.get(s.parent_key) if s.parent_key else None
        if parent is not None and parent.service != s.service:
            exact[(parent.service, s.service)] = exact.get((parent.service, s.service), 0) + 1
    assert set(exact) == {
        ("gateway", "orders"),
        ("orders", "inventory"),
        ("orders", "payment"),
        ("payment", "fraud"),
    }
    assert edges == exact  # every call counted exactly once, across ~25 OTLP requests
    nodes = {n["service"]: n for n in graph["nodes"]}
    assert nodes["payment"]["probable_origin"] is True
    assert nodes["orders"]["impact"] == "AFFECTED_UPSTREAM"


def _json(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload).encode()


async def test_control_scenario_reports_no_regression(
    api: TestClient, db: Sessions, producer: AsyncProducer, kafka_bootstrap: str
) -> None:
    tenant = await _tenant(db, ALL_SCOPES)
    deployment_id, _, _ = await _run_scenario(
        tg.control(),
        tenant=tenant,
        environment="qa",
        api=api,
        db=db,
        producer=producer,
        bootstrap=kafka_bootstrap,
    )
    final = (await _rows(db, deployment_id))[JobKind.FINAL]
    assert final.status == "HEALTHY", final.result["services"]
    assert final.probable_origin is None
    assert api.get("/api/v1/regressions", headers=tenant.auth).json()["items"] == []
    body = api.get(f"/api/v1/deployments/{deployment_id}/analysis", headers=tenant.auth).json()
    assert body["latest"]["status"] == "HEALTHY"


async def test_final_analysis_refuses_to_run_before_its_window_closes(db: Sessions) -> None:
    tenant = await _tenant(db, ["read"])
    ctx = TenantContext(tenant_id=tenant.id, principal=Principal.SYSTEM)
    now = datetime.now(UTC)
    async with db.begin() as s:
        rec = await record_deployment(
            s,
            ctx,
            DeploymentEvent(
                event_id=uuid.uuid4(),
                tenant_id=tenant.id,
                occurred_at=now,
                schema_version=2,
                service="payment",
                environment="staging",
                version="v2",
                previous_version="v1",
                commit_sha="abcdef1",
                deployed_at=now,
                deployer=None,
                ci_run_url=None,
                repository=None,
                pull_request=None,
                risk_assessment_id=None,
                labels=MappingProxyType({}),
            ),
        )
    assert rec.deployment_id is not None
    handler = AnalysisJobHandler(db)

    def job(kind: JobKind, deployment_id: uuid.UUID) -> ClaimedJob:
        return ClaimedJob(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            deployment_id=deployment_id,
            kind=kind,
            run_at=now,
            attempts=1,
            max_attempts=5,
            locked_by="t",
        )

    with pytest.raises(AnalysisNotDueError):
        await handler(job(JobKind.FINAL, rec.deployment_id))
    await handler(job(JobKind.FINAL, uuid.uuid4()))  # a deleted deployment is a no-op
    assert await _rows(db, rec.deployment_id) == {}
