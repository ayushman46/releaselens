"""End to end: POST/GET /api/v1/risk/assessments and /api/v1/ci-runs against a migrated
Postgres (testcontainers) and SYNTHETIC test bundles (``rl_api.testing.make_test_bundle``: tiny
XGBoost models trained on rl_ml's synthetic data; their scores mean nothing).

Covers the contract (docs/api/RISK_ASSESSMENTS.md): 201 with every field, idempotent replay 200,
503 without a model, 422 on unknown change facts, the cold-start path, cross-tenant 404, the
history the features are computed from, risk alerts, and the Check Run sent to GitHub.
"""

from __future__ import annotations

import base64
import json
import math
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select

from rl_api.apikeys import create_api_key
from rl_api.testing import Harness, make_test_bundle, saas_settings
from rl_core.features.change import BUILD_INPUT_FIELDS
from rl_core.features.schema import ALL_FEATURE_NAMES
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.github import link_installation
from rl_db.admin.identity import create_organization
from rl_db.models import NotificationLog, RiskAssessment
from rl_db.repositories import alerts
from rl_db.repositories import projects as catalog

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

REPO = "Acme/Payments"
FACTS: dict[str, Any] = {
    "src_churn": 120,
    "test_churn": 40,
    "files_added": 1,
    "files_deleted": 0,
    "files_modified": 6,
    "tests_added": 2,
    "tests_deleted": 0,
    "src_files": 5,
    "doc_files": 1,
    "other_files": 1,
    "sloc": 48211,
    "test_lines_per_kloc": 410.2,
    "test_cases_per_kloc": 21.5,
    "asserts_per_kloc": 55.0,
    "num_all_built_commits": 3,
    "team_size": 9,
    "by_core_team_member": True,
    "is_pr": True,
    "commits_on_files_touched": 14,
    "repo_age_days": 1203,
}
CONTRACT_FIELDS = {
    "assessment_id",
    "model_version",
    "feature_schema_version",
    "risk_score",
    "risk_band",
    "probability",
    "top_increasing",
    "top_reducing",
    "history",
    "details_url",
}


def _sha() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repository": REPO,
        "commit_sha": _sha(),
        "pull_request": 123,
        "branch": "feature/x",
        "event": "pull_request",
        "triggered_at": datetime.now(UTC).isoformat(),
        "ci_run_url": "https://github.com/acme/payments/actions/runs/1",
        "previous_built_sha": _sha(),
        "extractor": {"name": "releaselens-action", "version": "0.1.0"},
        "change_facts": dict(FACTS),
    }
    body.update(kw)
    return body


@dataclass
class Tenant:
    id: uuid.UUID
    write: dict[str, str]
    read: dict[str, str]

    @property
    def ctx(self) -> TenantContext:
        return TenantContext(tenant_id=self.id, principal=Principal.SYSTEM)


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_risk")


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("bundles")
    return {
        # every score is HIGH: exercises alerts and the Check Run title deterministically
        "main_high": make_test_bundle(root / "main", cuts=(0.0, 0.0, 2.0), probability=True),
        "cold": make_test_bundle(root / "cold", cold=True),
    }


@pytest.fixture(scope="module")
def app_key(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("gh") / "app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path, key


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def h(
    db_url: str, bundles: dict[str, Path], app_key: tuple[Path, Any]
) -> AsyncIterator[Harness]:
    harness = Harness(
        saas_settings(
            db_url,
            model_uri=str(bundles["main_high"]),
            cold_model_uri=str(bundles["cold"]),
            github_app_id="4242",
            github_app_private_key_file=str(app_key[0]),
        )
    )
    async with harness.running():
        yield harness


async def _tenant(h: Harness) -> Tenant:
    async with h.app.state.db_sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"risk-{uuid.uuid4().hex[:10]}")
        write = await create_api_key(s, org.id, "action", ["risk:write"])
        read = await create_api_key(s, org.id, "reader", ["read"])
    return Tenant(
        org.id,
        {"Authorization": f"Bearer {write.plaintext}"},
        {"Authorization": f"Bearer {read.plaintext}"},
    )


async def _report_runs(h: Harness, t: Tenant, n: int, *, fail_every: int = 3) -> None:
    async with h.client() as c:
        now = datetime.now(UTC)
        for i in range(n):
            start = now - timedelta(hours=n - i + 1)
            resp = await c.post(
                "/api/v1/ci-runs",
                headers=t.write,
                json={
                    "repository": REPO,
                    "commit_sha": _sha(),
                    "external_id": f"gha:{1000 + i}:1",
                    "status": "failed" if i % fail_every == 0 else "passed",
                    "started_at": start.isoformat(),
                    "finished_at": (start + timedelta(minutes=12)).isoformat(),
                    "branch": "main",
                    "source": "action",
                },
            )
            assert resp.status_code == 201, resp.text


# ----------------------------------------------------------------------------- the contract
async def test_assessment_with_history_returns_every_contract_field(h: Harness) -> None:
    t = await _tenant(h)
    await _report_runs(h, t, 7)
    body = _body()
    async with h.client() as c:
        resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert set(out) >= CONTRACT_FIELDS
    assert out["model_version"] == "synthetic_main_test/1"
    assert out["feature_schema_version"] == "1.0.0"
    assert out["history"] == {"finished_builds_used": 7, "cold_start": False}
    assert out["risk_band"] == "HIGH"  # the test bundle's cut-points make every score HIGH
    assert 0 <= out["risk_score"] <= 100
    assert out["probability"] is not None  # this bundle says the calibration gate passed
    assert 0.0 <= out["probability"] <= 1.0
    assert out["details_url"] == f"https://app.releaselens.test/app/risk/{out['assessment_id']}"
    assert "not the causes" in out["explanation_note"]
    for item in out["top_increasing"]:
        assert item["contribution"] > 0
        assert {"feature", "label", "value", "contribution", "text"} <= set(item)
    for item in out["top_reducing"]:
        assert item["contribution"] < 0
    assert len(out["top_increasing"]) <= 3
    assert len(out["top_reducing"]) <= 3

    async with h.client() as c:
        detail = (
            await c.get(f"/api/v1/risk/assessments/{out['assessment_id']}", headers=t.read)
        ).json()
    assert set(detail["features"]) == set(ALL_FEATURE_NAMES)
    assert detail["features"]["history_size"] == 7.0
    assert detail["features"]["log1p_src_churn"] == pytest.approx(math.log1p(120))
    assert detail["change_facts"] == FACTS
    assert detail["repository"] == "acme/payments"
    shap = detail["shap"]
    margin = shap["base_value"] + sum(shap["values"].values())
    assert 1 / (1 + math.exp(-margin)) == pytest.approx(detail["raw_score"], abs=1e-5)
    assert detail["extractor"] == {"name": "releaselens-action", "version": "0.1.0"}


async def test_replay_within_the_window_returns_the_stored_assessment(h: Harness) -> None:
    t = await _tenant(h)
    body = _body()
    async with h.client() as c:
        first = await c.post("/api/v1/risk/assessments", headers=t.write, json=body)
        replay = await c.post(
            "/api/v1/risk/assessments",
            headers=t.write,
            json=body | {"change_facts": {"src_churn": 1}},  # same key, different body
        )
        other_pr = await c.post(
            "/api/v1/risk/assessments", headers=t.write, json=body | {"pull_request": 124}
        )
        push = await c.post(
            "/api/v1/risk/assessments",
            headers=t.write,
            json=body | {"event": "push", "pull_request": None},
        )
    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["assessment_id"] == first.json()["assessment_id"]
    assert replay.json()["risk_score"] == first.json()["risk_score"]
    assert other_pr.status_code == 201
    assert push.status_code == 201
    assert len({first.json()["assessment_id"], other_pr.json()["assessment_id"]}) == 2


async def test_repository_without_finished_builds_uses_the_cold_start_model(h: Harness) -> None:
    t = await _tenant(h)
    async with h.client() as c:
        resp = await c.post(
            "/api/v1/risk/assessments", headers=t.write, json=_body(repository="acme/new-repo")
        )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["history"] == {"finished_builds_used": 0, "cold_start": True}
    assert out["model_version"] == "synthetic_cold_test/1"
    assert out["probability"] is None  # the cold bundle's calibration gate did not pass
    history_labels = {"Previous finished build failed", "Finished builds in the project history"}
    for item in out["top_increasing"] + out["top_reducing"]:
        assert item["label"] not in history_labels


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"change_facts": {**FACTS, "tr_status": 1}}, "unknown change facts"),
        ({"change_facts": {"src_churn": "120"}}, "change_facts"),
        ({"tenant_id": str(uuid.uuid4())}, "tenant_id"),
        ({"triggered_at": "2026-10-13T02:00:00"}, "triggered_at"),
        ({"event": "merge"}, "event"),
        ({"commit_sha": "xyz"}, "commit_sha"),
        ({"repository": "not a repo"}, "repository"),
    ],
)
async def test_invalid_requests_are_422(h: Harness, patch: dict[str, Any], needle: str) -> None:
    t = await _tenant(h)
    async with h.client() as c:
        resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=_body() | patch)
    assert resp.status_code == 422, resp.text
    assert needle in resp.text
    async with h.app.state.db_sessions() as s:
        rows = (
            await s.execute(select(RiskAssessment).where(RiskAssessment.tenant_id == t.id))
        ).all()
    assert rows == []


async def test_unknown_facts_are_422_and_missing_facts_are_unknown_not_zero(h: Harness) -> None:
    t = await _tenant(h)
    async with h.client() as c:
        resp = await c.post(
            "/api/v1/risk/assessments", headers=t.write, json=_body(change_facts={"src_churn": 4})
        )
        detail = await c.get(
            f"/api/v1/risk/assessments/{resp.json()['assessment_id']}", headers=t.read
        )
    assert resp.status_code == 201, resp.text
    features = detail.json()["features"]
    assert features["src_churn"] == 4.0
    assert features["test_churn"] is None  # missing → unknown, never 0
    assert features["total_churn"] is None


async def test_auth_scope_and_cross_tenant_isolation(h: Harness) -> None:
    a, b = await _tenant(h), await _tenant(h)
    async with h.client() as c:
        created = await c.post("/api/v1/risk/assessments", headers=a.write, json=_body())
        aid = created.json()["assessment_id"]
        assert (await c.post("/api/v1/risk/assessments", json=_body())).status_code == 401
        assert (
            await c.post("/api/v1/risk/assessments", headers=a.read, json=_body())
        ).status_code == 403
        assert (await c.get(f"/api/v1/risk/assessments/{aid}", headers=a.read)).status_code == 200
        cross = await c.get(f"/api/v1/risk/assessments/{aid}", headers=b.read)
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "not_found"
        listed = (await c.get("/api/v1/risk/assessments", headers=b.read)).json()
        assert listed["items"] == []
        mine = (await c.get("/api/v1/risk/assessments", headers=a.read)).json()
        assert [i["assessment_id"] for i in mine["items"]] == [aid]
        # the write key of B replaying A's request creates B's own assessment
        body = _body()
        ra = await c.post("/api/v1/risk/assessments", headers=a.write, json=body)
        rb = await c.post("/api/v1/risk/assessments", headers=b.write, json=body)
        assert (ra.status_code, rb.status_code) == (201, 201)
        assert ra.json()["assessment_id"] != rb.json()["assessment_id"]


async def test_list_pagination_and_filters(h: Harness) -> None:
    t = await _tenant(h)
    ids = []
    async with h.client() as c:
        for _ in range(3):
            resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=_body())
            ids.append(resp.json()["assessment_id"])
        page1 = (await c.get("/api/v1/risk/assessments?limit=2", headers=t.read)).json()
        page2 = (
            await c.get(
                f"/api/v1/risk/assessments?limit=2&cursor={page1['next_cursor']}", headers=t.read
            )
        ).json()
        bad = await c.get("/api/v1/risk/assessments?cursor=garbage", headers=t.read)
        none = (await c.get("/api/v1/risk/assessments?repository=acme/none", headers=t.read)).json()
    assert [i["assessment_id"] for i in page1["items"] + page2["items"]] == ids[::-1]
    assert page2["next_cursor"] is None
    assert bad.status_code == 400
    assert none["items"] == []


async def test_session_users_read_with_the_org_header(h: Harness) -> None:
    owner = await h.signup(f"risk-{uuid.uuid4().hex[:8]}@example.com")
    org_id = await h.create_org(owner)
    async with h.app.state.db_sessions.begin() as s:
        key = await create_api_key(s, org_id, "action", ["risk:write"])
    async with h.client() as c:
        created = await c.post(
            "/api/v1/risk/assessments",
            headers={"Authorization": f"Bearer {key.plaintext}"},
            json=_body(),
        )
    aid = created.json()["assessment_id"]
    ok = await owner.get(f"/api/v1/risk/assessments/{aid}", headers={"X-RL-Org": str(org_id)})
    no_header = await owner.get(f"/api/v1/risk/assessments/{aid}")
    other = await h.signup(f"risk-{uuid.uuid4().hex[:8]}@example.com")
    foreign = await other.get(f"/api/v1/risk/assessments/{aid}", headers={"X-RL-Org": str(org_id)})
    assert ok.status_code == 200
    assert no_header.status_code == 400
    assert foreign.status_code == 404


# ----------------------------------------------------------------------------- no model
async def test_no_model_loaded_is_503_and_nothing_is_stored(
    db_url: str, bundles: dict[str, Path]
) -> None:
    harness = Harness(saas_settings(db_url, model_uri=None, cold_model_uri=None))
    async with harness.running():
        t = await _tenant(harness)
        async with harness.client() as c:
            resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=_body())
            status = await c.get("/api/v1/risk/model", headers=t.read)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "no_model_loaded"
    assert "risk_score" not in resp.text
    assert status.json()["loaded"] == {}

    # champion loaded but no cold-start model: a repository without history gets 503 too
    harness = Harness(
        saas_settings(db_url, model_uri=str(bundles["main_high"]), cold_model_uri=None)
    )
    async with harness.running():
        t = await _tenant(harness)
        async with harness.client() as c:
            resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=_body())
    assert resp.status_code == 503
    assert resp.json()["error"]["details"] == {"model": "cold"}
    async with harness.app.state.db_sessions() as s:
        rows = (
            await s.execute(select(RiskAssessment).where(RiskAssessment.tenant_id == t.id))
        ).all()
    assert rows == []


async def test_model_status_endpoint(h: Harness) -> None:
    t = await _tenant(h)
    async with h.client() as c:
        out = (await c.get("/api/v1/risk/model", headers=t.read)).json()
    assert out["loaded"]["main"]["model_version"] == "synthetic_main_test/1"
    assert out["loaded"]["main"]["probability_exposed"] is True
    assert out["loaded"]["cold"]["probability_exposed"] is False
    assert len(out["loaded"]["cold"]["checksum"]) == 64


# ----------------------------------------------------------------------------- side effects
def _jwt_claims(token: str, public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    head, payload, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    public_key.verify(
        base64.urlsafe_b64decode(pad(sig)),
        f"{head}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT"}
    claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(pad(payload)))
    return claims


async def test_check_run_and_risk_alert_for_a_linked_repository(
    h: Harness, app_key: tuple[Path, rsa.RSAPrivateKey]
) -> None:
    t = await _tenant(h)
    installation = int(uuid.uuid4().int % 10**9) + 1
    async with h.app.state.db_sessions.begin() as s:
        await link_installation(
            s, installation_id=installation, tenant_id=t.id, account_login="Acme"
        )
        project = await catalog.create_project(
            s, t.ctx, name="Payments", slug="payments", github_owner="acme", github_repo="payments"
        )
        await alerts.create_alert_rule(
            s,
            t.ctx,
            project_id=project.id,
            values={"name": "risk", "on_risk": True, "email_recipients": ["oncall@example.com"]},
        )
    h.github.check_runs.clear()
    h.github.requests.clear()
    body = _body()
    async with h.client() as c:
        resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=body)
        replay = await c.post("/api/v1/risk/assessments", headers=t.write, json=body)
    assert resp.status_code == 201, resp.text
    assert replay.status_code == 200
    out = resp.json()

    # one Check Run (the replay posts none), advisory, with the score and the disclaimer
    assert len(h.github.check_runs) == 1
    run = h.github.check_runs[0]
    assert run["name"] == "ReleaseLens risk"
    assert run["head_sha"] == body["commit_sha"]
    assert run["status"] == "completed"
    assert run["conclusion"] == "neutral"
    assert run["external_id"] == out["assessment_id"]
    assert run["details_url"] == out["details_url"]
    assert run["output"]["title"] == f"ReleaseLens: Risk HIGH, {out['risk_score']}/100"
    summary = run["output"]["summary"]
    assert "not a production outage probability" in summary
    assert "not the causes of a failure" in summary
    for item in out["top_increasing"]:
        assert item["text"].split(" is ")[0] in summary

    token_req, check_req = [r for r in h.github.requests if r.url.host == "api.github.com"][-2:]
    assert token_req.url.path == f"/app/installations/{installation}/access_tokens"
    claims = _jwt_claims(
        token_req.headers["authorization"].removeprefix("Bearer "), app_key[1].public_key()
    )
    assert claims["iss"] == "4242"
    assert 0 < claims["exp"] - claims["iat"] <= 600
    assert check_req.url.path == "/repos/acme/payments/check-runs"
    assert check_req.headers["authorization"] == "Bearer ghs_installation_test"
    assert check_req.headers["x-github-api-version"] == "2022-11-28"

    # the HIGH band queued exactly one e-mail alert for the project's rule
    async with h.app.state.db_sessions() as s:
        rows = (
            (await s.execute(select(NotificationLog).where(NotificationLog.tenant_id == t.id)))
            .scalars()
            .all()
        )
    assert [(r.kind, r.channel, str(r.subject_id)) for r in rows] == [
        ("risk", "email", out["assessment_id"])
    ]
    assert rows[0].payload["band"] == "HIGH"
    assert rows[0].payload["repository"] == "acme/payments"


async def test_check_run_failure_never_fails_the_assessment(h: Harness) -> None:
    t = await _tenant(h)
    async with h.app.state.db_sessions.begin() as s:
        await link_installation(
            s,
            installation_id=int(uuid.uuid4().int % 10**9) + 1,
            tenant_id=t.id,
            account_login="acme",
        )
    h.github.check_run_status = 500
    try:
        async with h.client() as c:
            resp = await c.post("/api/v1/risk/assessments", headers=t.write, json=_body())
    finally:
        h.github.check_run_status = 201
    assert resp.status_code == 201, resp.text


async def test_unlinked_repository_gets_no_check_run(h: Harness) -> None:
    t = await _tenant(h)
    h.github.check_runs.clear()
    async with h.client() as c:
        resp = await c.post(
            "/api/v1/risk/assessments", headers=t.write, json=_body(repository="other/repo")
        )
    assert resp.status_code == 201
    assert h.github.check_runs == []


# ----------------------------------------------------------------------------- ci-runs
async def test_ci_runs_ingestion_is_idempotent_and_validated(h: Harness) -> None:
    t = await _tenant(h)
    start = datetime.now(UTC) - timedelta(hours=1)
    run = {
        "repository": REPO,
        "commit_sha": _sha(),
        "external_id": "gha:77:1",
        "status": "running",
        "started_at": start.isoformat(),
    }
    async with h.client() as c:
        created = await c.post("/api/v1/ci-runs", headers=t.write, json=run)
        done = await c.post(
            "/api/v1/ci-runs",
            headers=t.write,
            json=run
            | {"status": "failed", "finished_at": (start + timedelta(minutes=5)).isoformat()},
        )
        again = await c.post(
            "/api/v1/ci-runs",
            headers=t.write,
            json=run
            | {"status": "passed", "finished_at": (start + timedelta(minutes=5)).isoformat()},
        )
        no_finish = await c.post(
            "/api/v1/ci-runs", headers=t.write, json=run | {"status": "passed"}
        )
        backwards = await c.post(
            "/api/v1/ci-runs",
            headers=t.write,
            json=run
            | {"status": "passed", "finished_at": (start - timedelta(minutes=5)).isoformat()},
        )
        read_key = await c.post("/api/v1/ci-runs", headers=t.read, json=run)
        listed = await c.get(f"/api/v1/ci-runs?repository={REPO}", headers=t.read)
    assert (created.status_code, created.json()["outcome"]) == (201, "created")
    assert (done.status_code, done.json()["outcome"]) == (200, "updated")
    assert (again.status_code, again.json()["outcome"]) == (200, "duplicate")
    assert no_finish.status_code == 422
    assert backwards.status_code == 422
    assert read_key.status_code == 403
    runs = listed.json()
    assert [(r["status"], r["duration_s"]) for r in runs] == [("failed", 300.0)]
    assert set(FACTS) <= set(BUILD_INPUT_FIELDS)
