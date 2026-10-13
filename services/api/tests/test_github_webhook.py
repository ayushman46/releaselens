"""GitHub App webhook against a migrated Postgres, with SIGNED fixture deliveries (no GitHub).

Signature check (constant time), delivery dedupe, installation → tenant mapping, and the effect
of every handled event: workflow_run / check_suite → ci_runs, pull_request → stored state,
deployment_status → a deployment event in the outbox, installation suspend/delete.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from rl_api.github_app import compute_signature
from rl_api.testing import Harness, saas_settings
from rl_core.events import DEPLOYMENT_EVENTS, parse_envelope
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.github import link_installation
from rl_db.admin.identity import create_organization
from rl_db.models import CiRun, GithubPullRequest, OutboxEvent, WebhookDelivery
from rl_db.repositories import projects as catalog

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

SECRET = "whsec-test-0123456789abcdef"  # fixture value gitleaks:allow
URL = "/api/v1/integrations/github/webhook"
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_gh")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def h(db_url: str) -> AsyncIterator[Harness]:
    harness = Harness(saas_settings(db_url, github_webhook_secret=SECRET, github_app_id="4242"))
    async with harness.running():
        yield harness


async def _linked(h: Harness, login: str = "acme") -> tuple[TenantContext, int]:
    inst = int(uuid.uuid4().int % 10**9) + 1
    async with h.app.state.db_sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"gh-{uuid.uuid4().hex[:10]}")
        await link_installation(s, installation_id=inst, tenant_id=org.id, account_login=login)
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM), inst


async def _send(
    h: Harness,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str | None = None,
    secret: str = SECRET,
    signature: str | None = None,
) -> Any:
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "X-Hub-Signature-256": signature or compute_signature(secret, body),
    }
    async with h.client() as c:
        return await c.post(URL, content=body, headers=headers)


def _repo(name: str = "Acme/Payments") -> dict[str, Any]:
    return {"full_name": name, "owner": {"login": name.split("/")[0]}}


def _workflow_run(
    inst: int, *, run_id: int, action: str = "completed", **kw: Any
) -> dict[str, Any]:
    run = {
        "id": run_id,
        "run_attempt": 1,
        "event": "pull_request",
        "head_sha": "A" * 40,
        "head_branch": "feature",
        "status": "completed" if action == "completed" else "in_progress",
        "conclusion": "failure" if action == "completed" else None,
        "created_at": _iso(NOW),
        "run_started_at": _iso(NOW),
        "updated_at": _iso(NOW + timedelta(minutes=9)),
        "pull_requests": [{"number": 12}],
    }
    run.update(kw)
    return {
        "action": action,
        "workflow_run": run,
        "repository": _repo(),
        "installation": {"id": inst},
    }


async def _ci_runs(h: Harness, ctx: TenantContext) -> list[CiRun]:
    async with h.app.state.db_sessions() as s:
        stmt = select(CiRun).where(CiRun.tenant_id == ctx.tenant_id).order_by(CiRun.external_id)
        return list((await s.execute(stmt)).scalars())


# ----------------------------------------------------------------------------- the gate
async def test_unconfigured_app_answers_503(db_url: str) -> None:
    harness = Harness(saas_settings(db_url))
    async with harness.running():
        resp = await _send(harness, "ping", {"zen": "hi"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "github_app_not_configured"


async def test_bad_or_missing_signatures_are_401(h: Harness) -> None:
    ctx, inst = await _linked(h)
    payload = _workflow_run(inst, run_id=1)
    wrong_secret = await _send(h, "workflow_run", payload, secret="not-the-secret")
    tampered = await _send(h, "workflow_run", payload, signature=compute_signature(SECRET, b"{}"))
    garbage = await _send(h, "workflow_run", payload, signature="sha1=abc")
    for resp in (wrong_secret, tampered, garbage):
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_signature"
    assert await _ci_runs(h, ctx) == []


async def test_ping_and_malformed_headers(h: Harness) -> None:
    assert (await _send(h, "ping", {"zen": "Keep it logically awesome."})).json() == {
        "status": "pong"
    }
    bad_delivery = await _send(h, "ping", {}, delivery="../../etc")
    assert bad_delivery.status_code == 400


async def test_unknown_installation_is_acknowledged_and_ignored(h: Harness) -> None:
    resp = await _send(h, "workflow_run", _workflow_run(999_999_999_9, run_id=2))
    assert resp.status_code == 202
    assert resp.json() == {"status": "ignored", "outcome": "ignored:unknown_installation"}


# ----------------------------------------------------------------------------- ci runs
async def test_workflow_runs_become_ci_runs_once(h: Harness) -> None:
    ctx, inst = await _linked(h)
    started = await _send(h, "workflow_run", _workflow_run(inst, run_id=10, action="in_progress"))
    delivery = str(uuid.uuid4())
    done = await _send(h, "workflow_run", _workflow_run(inst, run_id=10), delivery=delivery)
    redelivered = await _send(h, "workflow_run", _workflow_run(inst, run_id=10), delivery=delivery)
    timed_out = await _send(
        h, "workflow_run", _workflow_run(inst, run_id=11, conclusion="timed_out")
    )
    cancelled = await _send(
        h, "workflow_run", _workflow_run(inst, run_id=12, conclusion="cancelled")
    )
    scheduled = await _send(h, "workflow_run", _workflow_run(inst, run_id=13, event="schedule"))
    assert started.json()["outcome"] == "ci_run:created"
    assert done.json()["outcome"] == "ci_run:updated"
    assert (redelivered.status_code, redelivered.json()) == (200, {"status": "duplicate"})
    assert scheduled.json()["outcome"] == "ignored:trigger_schedule"
    assert timed_out.status_code == cancelled.status_code == 202
    runs = await _ci_runs(h, ctx)
    assert [(r.external_id, r.status) for r in runs] == [
        ("gha:10:1", "failed"),
        ("gha:11:1", "errored"),
        ("gha:12:1", "canceled"),
    ]
    first = runs[0]
    assert first.repository == "acme/payments"
    assert first.commit_sha == "a" * 40
    assert (first.is_pr, first.pull_request, first.source) == (True, 12, "github_app")
    assert first.duration_s == 540.0
    async with h.app.state.db_sessions() as s:
        row = await s.get(WebhookDelivery, delivery)
    assert row is not None
    assert (row.tenant_id, row.outcome) == (ctx.tenant_id, "ci_run:updated")


async def test_check_suites_of_other_ci_apps_only(h: Harness) -> None:
    ctx, inst = await _linked(h)

    def suite(suite_id: int, slug: str, app_id: int) -> dict[str, Any]:
        return {
            "action": "completed",
            "check_suite": {
                "id": suite_id,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "conclusion": "success",
                "created_at": _iso(NOW),
                "updated_at": _iso(NOW + timedelta(minutes=3)),
                "pull_requests": [],
                "app": {"id": app_id, "slug": slug},
            },
            "repository": _repo(),
            "installation": {"id": inst},
        }

    gha = await _send(h, "check_suite", suite(1, "github-actions", 15368))
    own = await _send(h, "check_suite", suite(2, "releaselens", 4242))
    circle = await _send(h, "check_suite", suite(3, "circleci-checks", 18001))
    assert gha.json()["outcome"] == "ignored:github_actions_suite"
    assert own.json()["outcome"] == "ignored:own_suite"
    assert circle.json()["outcome"] == "ci_run:created"
    assert [(r.external_id, r.status) for r in await _ci_runs(h, ctx)] == [
        ("check_suite:3", "passed")
    ]


async def test_ci_runs_from_webhooks_feed_the_risk_history(h: Harness) -> None:
    """A webhook-reported run and the same run reported by the API count once."""
    ctx, inst = await _linked(h)
    await _send(h, "workflow_run", _workflow_run(inst, run_id=50))
    from rl_api.apikeys import create_api_key

    async with h.app.state.db_sessions.begin() as s:
        key = await create_api_key(s, ctx.tenant_id, "ci", ["risk:write"])
    async with h.client() as c:
        resp = await c.post(
            "/api/v1/ci-runs",
            headers={"Authorization": f"Bearer {key.plaintext}"},
            json={
                "repository": "acme/payments",
                "commit_sha": "a" * 40,
                "external_id": "gha:50:1",
                "status": "failed",
                "started_at": _iso(NOW),
                "finished_at": _iso(NOW + timedelta(minutes=9)),
            },
        )
    assert resp.json()["outcome"] == "duplicate"
    assert len(await _ci_runs(h, ctx)) == 1


# ----------------------------------------------------------------------------- other events
async def test_pull_request_state_is_stored_without_text(h: Harness) -> None:
    ctx, inst = await _linked(h)
    payload = {
        "action": "opened",
        "number": 7,
        "pull_request": {
            "number": 7,
            "state": "open",
            "draft": False,
            "merged": False,
            "title": "secret project codename",
            "body": "do not store me",
            "user": {"login": "someone"},
            "head": {"sha": "C" * 40, "ref": "feature"},
            "base": {"sha": "d" * 40, "ref": "main"},
            "updated_at": _iso(NOW),
        },
        "repository": _repo(),
        "installation": {"id": inst},
    }
    resp = await _send(h, "pull_request", payload)
    assert resp.json()["outcome"] == "pull_request:stored"
    async with h.app.state.db_sessions() as s:
        pr = (
            await s.execute(
                select(GithubPullRequest).where(GithubPullRequest.tenant_id == ctx.tenant_id)
            )
        ).scalar_one()
    assert (pr.number, pr.head_sha, pr.base_ref, pr.state) == (7, "c" * 40, "main", "open")
    assert "codename" not in str(pr.__dict__)


async def test_deployment_status_success_queues_a_deployment_event(h: Harness) -> None:
    ctx, inst = await _linked(h)
    async with h.app.state.db_sessions.begin() as s:
        project = await catalog.create_project(
            s, ctx, name="Payments", slug="payments", github_owner="acme", github_repo="payments"
        )
        await catalog.create_service(s, ctx, project_id=project.id, name="payment")

    def status(status_id: int, state: str) -> dict[str, Any]:
        return {
            "action": "created",
            "deployment_status": {"id": status_id, "state": state, "created_at": _iso(NOW)},
            "deployment": {"id": 9, "sha": "E" * 40, "environment": "production", "ref": "main"},
            "repository": _repo(),
            "installation": {"id": inst},
        }

    pending = await _send(h, "deployment_status", status(1, "in_progress"))
    ok = await _send(h, "deployment_status", status(2, "success"))
    assert pending.json()["outcome"] == "ignored:state_in_progress"
    assert ok.json()["outcome"] == "deployment:queued"
    async with h.app.state.db_sessions() as s:
        rows = (
            (await s.execute(select(OutboxEvent).where(OutboxEvent.tenant_id == ctx.tenant_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].topic == DEPLOYMENT_EVENTS
    event = parse_envelope(json.dumps(rows[0].payload).encode())
    assert (event.service, event.environment, event.commit_sha) == (
        "payment",
        "production",
        "e" * 40,
    )
    assert event.tenant_id == ctx.tenant_id
    assert event.repository == "acme/payments"


async def test_installation_suspension_stops_processing(h: Harness) -> None:
    ctx, inst = await _linked(h)
    suspended = await _send(h, "installation", {"action": "suspend", "installation": {"id": inst}})
    ignored = await _send(h, "workflow_run", _workflow_run(inst, run_id=70))
    resumed = await _send(h, "installation", {"action": "unsuspend", "installation": {"id": inst}})
    accepted = await _send(h, "workflow_run", _workflow_run(inst, run_id=71))
    assert suspended.json()["outcome"] == "installation:suspend"
    assert ignored.json()["outcome"] == "ignored:installation_suspended"
    assert resumed.json()["outcome"] == "installation:unsuspend"
    assert accepted.json()["outcome"] == "ci_run:created"
    assert [r.external_id for r in await _ci_runs(h, ctx)] == ["gha:71:1"]


async def test_malformed_payloads_are_acknowledged_not_applied(h: Harness) -> None:
    ctx, inst = await _linked(h)
    payload = _workflow_run(inst, run_id=80)
    del payload["workflow_run"]["head_sha"]
    resp = await _send(h, "workflow_run", payload)
    assert resp.status_code == 202
    assert resp.json()["outcome"].startswith("ignored:malformed")
    assert await _ci_runs(h, ctx) == []
    not_json = await _send(h, "workflow_run", [1, 2])  # type: ignore[arg-type]
    assert not_json.status_code == 400
