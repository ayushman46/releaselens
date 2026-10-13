"""``AnalysisOut.result`` is typed (OpenAPI) without changing the JSON the API returned before."""

from __future__ import annotations

import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from rl_api.app import create_app
from rl_api.routers.analysis import AnalysisOut, DeploymentAnalysis
from rl_api.settings import Settings
from rl_core.regression.analysis import ENGINE_VERSION, RegressionAnalysis, analyse_deployment
from rl_core.regression.evidence import DeploymentRef
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind, StatusCode, datetime_to_ns
from rl_db.models import JobKind

T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
TENANT = str(uuid.UUID(int=7))


def _spans() -> list[Span]:
    """gateway → payment, 400 requests before and after T; payment slows down after T."""
    rng = random.Random(3)
    spans: list[Span] = []
    for i in range(800):
        before = i < 400
        at = (
            T - timedelta(minutes=25, seconds=-i * 3)
            if before
            else T + timedelta(minutes=3, seconds=(i - 400) * 1.5)
        )
        start = datetime_to_ns(at)
        pay = rng.lognormvariate(3.0, 0.2) + (0 if before else 80)
        trace = f"{i + 1:032x}"
        for sid, parent, service, kind, dur, err in (
            (1, None, "gateway", SpanKind.SERVER, pay + 5, False),
            (2, 1, "gateway", SpanKind.CLIENT, pay + 2, False),
            (3, 2, "payment", SpanKind.SERVER, pay, not before and i % 10 == 0),
        ):
            spans.append(
                Span(
                    tenant_id=TENANT,
                    trace_id=trace,
                    span_id=f"{sid:016x}",
                    parent_span_id=None if parent is None else f"{parent:016x}",
                    service=service,
                    service_version="v2" if not before and service == "payment" else "v1",
                    environment="staging",
                    name=service,
                    kind=kind,
                    start_ns=start,
                    end_ns=start + round(dur * NS_PER_MS),
                    status_code=StatusCode.ERROR if err else StatusCode.UNSET,
                )
            )
    return spans


def _row(result: dict[str, Any], engine_version: str = ENGINE_VERSION) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        deployment_id=uuid.uuid4(),
        kind=JobKind.FINAL,
        status="REGRESSED",
        severity_score=50.0,
        severity_band="HIGH",
        probable_origin="payment",
        confidence="HIGH",
        engine_version=engine_version,
        windows={"profile": "default"},
        sampling={"sampled": False},
        created_at=T,
        updated_at=T,
        result=result,
    )


def _wire(out: AnalysisOut) -> Any:
    """What FastAPI sends for the response model."""
    page = DeploymentAnalysis(deployment_id=out.deployment_id, latest=out, analyses=[out])
    return json.loads(page.model_dump_json())["latest"]["result"]


def test_engine_result_is_typed_and_its_json_is_unchanged() -> None:
    analysis = analyse_deployment(
        _spans(),
        DeploymentRef(service="payment", environment="staging", deployed_at=T, tenant_id=TENANT),
        seed=0,
    )
    assert analysis.services  # a real result with evidence
    stored = json.loads(json.dumps(analysis.model_dump(mode="json")))  # what jsonb holds
    out = AnalysisOut.of(_row(stored))  # type: ignore[arg-type]
    assert isinstance(out.result, RegressionAnalysis)
    assert _wire(out) == stored


def test_result_the_model_does_not_describe_is_passed_through_unchanged() -> None:
    partial = {"deployment": {"service": "payment"}, "services": [{"service": "a"}]}
    future = {"engine_version": "regression-engine/2.0.0", "new_field": [1, 2, 3]}
    for raw, version in ((partial, ENGINE_VERSION), (future, "regression-engine/2.0.0")):
        out = AnalysisOut.of(_row(raw, version))  # type: ignore[arg-type]
        assert isinstance(out.result, dict)
        assert _wire(out) == raw


def test_openapi_types_the_result_and_documents_the_new_reads() -> None:
    app = create_app(
        Settings(
            env="test",
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
            kafka_bootstrap="127.0.0.1:1",
            otel_enabled=False,
        )
    )
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert schemas["AnalysisOut"]["properties"]["result"]["$ref"] == (
        "#/components/schemas/RegressionAnalysis"
    )
    assert {"engine_version", "services", "triage", "blast_radius"} <= set(
        schemas["RegressionAnalysis"]["properties"]
    )
    # Every component has its plain name (no module-qualified duplicates for the web client).
    assert not [n for n in schemas if "__" in n]
    latest = schemas["DeploymentOut"]["properties"]["latest_analysis"]
    assert {"$ref": "#/components/schemas/LatestAnalysisOut"} in latest["anyOf"]
    assert set(schemas["LatestAnalysisOut"]["required"]) >= {
        "kind",
        "status",
        "severity_score",
        "severity_band",
        "probable_origin",
        "confidence",
    }
    paths = spec["paths"]
    for path in (
        "/api/v1/deployments/{deployment_id}/timeseries",
        "/api/v1/environments",
        "/api/v1/stats/overview",
        "/api/v1/auth/github/status",
    ):
        assert "get" in paths[path], path
    for path in ("/api/v1/deployments", "/api/v1/regressions", "/api/v1/risk/assessments"):
        names = {p["name"] for p in paths[path]["get"]["parameters"]}
        assert "since" in names, path
    ci = {p["name"]: p for p in paths["/api/v1/ci-runs"]["get"]["parameters"]}
    assert not ci["repository"]["required"]
    assert "commit_sha" in ci
    assert schemas["DeploymentTimeseries"]["properties"]["latency_approximate"]["const"] is True
