"""Notifier against real Postgres (and Kafka for the consumer): analysis.results →
notification_log → Slack (httpx MockTransport) / e-mail (recording sender)."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from confluent_kafka.admin import AdminClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import dlq
from rl_core.events.analysis import AnalysisSummary, encode_analysis, make_analysis_envelope
from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.identity import create_organization
from rl_db.models import NotificationLog, Organization, Plan
from rl_db.repositories import alerts, catalog
from rl_db.secretbox import SecretBox, slack_webhook_context
from rl_worker.maintenance import Maintenance
from rl_worker.notify import enqueue_for_analysis, notify_risk
from rl_worker.notify.channels import DeliveryError, RecordingEmailSender, SlackSender
from rl_worker.notify.consumer import AnalysisResultsConsumer
from rl_worker.notify.dispatcher import NotificationDispatcher
from rl_worker.retry import Backoff
from rl_worker.testing import headers_of, produce_raw, read_topic, wait_until

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
BOX = SecretBox("w" * 40)  # gitleaks:allow - test-only master key
SLACK_URL = "https://hooks.slack.com/services/T0/B0/notarealtoken"


@dataclass
class SlackMock:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status, headers=self.headers, text="ok" if self.status == 200 else "err"
        )

    def sender(self) -> SlackSender:
        return SlackSender(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=False)
        )


@dataclass
class Tenant:
    """The worker test database is shared, and the dispatcher serves every tenant, so each
    tenant gets its own webhook URL and recipient: assertions filter on them."""

    ctx: TenantContext
    project_id: uuid.UUID
    rule_id: uuid.UUID
    slack_url: str | None
    email: str

    def slack_calls(self, slack: SlackMock) -> list[httpx.Request]:
        return [r for r in slack.requests if str(r.url) == self.slack_url]

    def emails(self, sender: RecordingEmailSender) -> list[tuple[tuple[str, ...], str, str]]:
        return [m for m in sender.sent if self.email in m[0]]


async def _tenant(
    sessions: Sessions,
    *,
    plan: Plan = Plan.TEAM,
    service: str = "payment",
    slack_url: str | None = "",
    **rule: Any,
) -> Tenant:
    tag = uuid.uuid4().hex[:12]
    if slack_url == "":
        slack_url = f"{SLACK_URL}{tag}"
    email = f"oncall-{tag}@example.com"
    async with sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:10]}")
        org.plan = plan
        ctx = TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)
        project = await catalog.create_project(s, ctx, name="Shop", slug="shop")
        await catalog.create_service(s, ctx, project_id=project.id, name=service)
        rule_id = uuid7()
        values: dict[str, Any] = {
            "id": rule_id,
            "name": "on-call",
            "email_recipients": [email],
            **rule,
        }
        if slack_url is not None:
            values["slack_webhook_ciphertext"] = BOX.encrypt(
                slack_url, context=slack_webhook_context(org.id, rule_id)
            )
            values["slack_webhook_hint"] = "x"
        await alerts.create_alert_rule(s, ctx, project_id=project.id, values=values)
    return Tenant(ctx=ctx, project_id=project.id, rule_id=rule_id, slack_url=slack_url, email=email)


def _envelope(
    tenant: uuid.UUID,
    *,
    band: str | None = "HIGH",
    kind: str = "final",
    service: str = "payment",
    deployment_id: uuid.UUID | None = None,
) -> Any:
    summary = AnalysisSummary(
        deployment_id=deployment_id or uuid7(),
        analysis_id=uuid7(),
        kind=kind,  # type: ignore[arg-type]
        service=service,
        environment="production",
        version="v2",
        previous_version="v1",
        status="REGRESSED" if band in ("HIGH", "CRITICAL") else "WARNING",
        severity_score=58.6 if band else None,
        severity_band=band,  # type: ignore[arg-type]
        probable_origin="payment",
        confidence="HIGH",
        affected_services=["orders"],
        engine_version="regression-engine/1.0.0",
        completed_at=datetime.now(UTC),
    )
    return make_analysis_envelope(tenant_id=tenant, summary=summary)


async def _rows(sessions: Sessions, tenant: Tenant) -> list[NotificationLog]:
    async with sessions() as s:
        result = await s.execute(
            select(NotificationLog)
            .where(NotificationLog.tenant_id == tenant.ctx.tenant_id)
            .order_by(NotificationLog.channel)
        )
        return list(result.scalars())


def _dispatcher(
    sessions: Sessions,
    slack: SlackMock,
    email: RecordingEmailSender,
    *,
    allow_any_slack_host: bool = False,
    box: SecretBox = BOX,
    worker_id: str = "test-dispatcher",
) -> NotificationDispatcher:
    return NotificationDispatcher(
        sessions,
        worker_id=worker_id,
        slack=slack.sender(),
        email=email,
        secret_box=box,
        web_base_url="https://app.releaselens.test",
        allow_any_slack_host=allow_any_slack_host,
        backoff=Backoff(base_s=0.01, cap_s=0.02, max_attempts=3),
    )


async def _dispatch_until_idle(dispatcher: NotificationDispatcher) -> None:
    for _ in range(20):
        if await dispatcher.run_once() == 0:
            return


# ----------------------------------------------------------------------------- enqueue
async def test_high_regression_queues_one_row_per_channel_idempotently(sessions: Sessions) -> None:
    t = await _tenant(sessions)
    env = _envelope(t.ctx.tenant_id)
    async with sessions.begin() as s:
        first = await enqueue_for_analysis(s, env)
    async with sessions.begin() as s:
        again = await enqueue_for_analysis(s, env)  # redelivery
    later = _envelope(t.ctx.tenant_id, band="CRITICAL", deployment_id=env.payload.deployment_id)
    async with sessions.begin() as s:
        superseding = await enqueue_for_analysis(s, later)  # final after early: same subject
    assert (first.created, again.created, superseding.created) == (2, 0, 0)
    rows = await _rows(sessions, t)
    assert [(r.channel, r.kind, r.status) for r in rows] == [
        ("email", "regression", "pending"),
        ("slack", "regression", "pending"),
    ]
    assert rows[0].subject_id == env.payload.deployment_id
    assert "hooks.slack.com" not in json.dumps(rows[1].payload)  # payload holds no secret


@pytest.mark.parametrize("band", [None, "HEALTHY", "WARNING"])
async def test_bands_below_high_queue_nothing(sessions: Sessions, band: str | None) -> None:
    t = await _tenant(sessions)
    async with sessions.begin() as s:
        result = await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id, band=band))
    assert result.created == 0


async def test_rule_minimum_band_and_unregistered_services(sessions: Sessions) -> None:
    t = await _tenant(sessions, regression_min_band="CRITICAL")
    async with sessions.begin() as s:
        high = await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id, band="HIGH"))
        unknown = await enqueue_for_analysis(
            s, _envelope(t.ctx.tenant_id, band="CRITICAL", service="not-registered")
        )
        critical = await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id, band="CRITICAL"))
    assert (high.created, unknown.created, critical.created) == (0, 0, 2)


async def test_notify_risk_hook(sessions: Sessions) -> None:
    t = await _tenant(sessions, on_risk=True, risk_min_band="HIGH", slack_url=None)
    assessment = uuid7()
    async with sessions.begin() as s:
        low = await notify_risk(
            s, t.ctx, risk_assessment_id=uuid7(), band="MEDIUM", service="payment"
        )
        queued = await notify_risk(
            s, t.ctx, risk_assessment_id=assessment, band="HIGH", service="payment", risk_score=81
        )
        dup = await notify_risk(
            s, t.ctx, risk_assessment_id=assessment, band="HIGH", service="payment"
        )
    assert (low.created, queued.created, dup.created) == (0, 1, 0)
    rows = await _rows(sessions, t)
    assert [(r.kind, r.channel, r.subject_id) for r in rows] == [("risk", "email", assessment)]


# ----------------------------------------------------------------------------- dispatch
async def test_dispatch_sends_slack_and_email_once(sessions: Sessions) -> None:
    t = await _tenant(sessions)
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id))
    slack, email = SlackMock(), RecordingEmailSender()
    # Two dispatchers race for the same rows: SKIP LOCKED gives each row to one of them.
    d1 = _dispatcher(sessions, slack, email, worker_id="d1")
    d2 = _dispatcher(sessions, slack, email, worker_id="d2")
    await asyncio.gather(_dispatch_until_idle(d1), _dispatch_until_idle(d2))
    await _dispatch_until_idle(d1)
    [call] = t.slack_calls(slack)
    body = json.loads(call.content)
    assert body["text"].startswith("Regression HIGH")
    [(to, subject, text)] = t.emails(email)
    assert to == (t.email,)
    assert subject.startswith("[ReleaseLens] Regression HIGH")
    assert "https://app.releaselens.test/app/deployments/" in text
    rows = await _rows(sessions, t)
    assert {r.status for r in rows} == {"sent"}
    assert all(r.sent_at is not None and r.attempts == 1 for r in rows)


async def test_transient_failures_retry_with_backoff_then_dead_letter(sessions: Sessions) -> None:
    t = await _tenant(sessions)
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id))
    slack = SlackMock(status=503)
    email = RecordingEmailSender(fail_with=DeliveryError("smtp 451", permanent=False))
    dispatcher = _dispatcher(sessions, slack, email)
    for _ in range(8):  # max_attempts is 6; backoff floor is 1 s, so pull next_attempt_at in
        await dispatcher.run_once()
        async with sessions.begin() as s:
            await s.execute(
                update(NotificationLog)
                .where(
                    NotificationLog.tenant_id == t.ctx.tenant_id, NotificationLog.status == "failed"
                )
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    rows = await _rows(sessions, t)
    assert {r.status for r in rows} == {"dead"}
    assert all(r.attempts == r.max_attempts == 6 for r in rows)
    assert len(t.slack_calls(slack)) == 6
    assert rows[1].last_error is not None
    assert "503" in rows[1].last_error


async def test_slack_retry_after_is_honoured(sessions: Sessions) -> None:
    t = await _tenant(sessions, email_recipients=[])
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id))
    dispatcher = _dispatcher(
        sessions, SlackMock(status=429, headers={"Retry-After": "120"}), RecordingEmailSender()
    )
    await dispatcher.run_once()
    [row] = await _rows(sessions, t)
    assert row.status == "failed"
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=100)


async def test_permanent_failures_go_dead_at_once(sessions: Sessions) -> None:
    t = await _tenant(sessions, email_recipients=[])
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id))
    await _dispatcher(sessions, SlackMock(status=404), RecordingEmailSender()).run_once()
    [row] = await _rows(sessions, t)
    assert (row.status, row.attempts) == ("dead", 1)


async def test_stored_url_is_revalidated_before_sending(sessions: Sessions) -> None:
    # A URL that slipped past validation on save (or a row edited in the database) is
    # still never requested: outside dev/test only hooks.slack.com is allowed.
    evil = await _tenant(sessions, email_recipients=[], slack_url="http://169.254.169.254/latest")
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(evil.ctx.tenant_id))
    slack = SlackMock()
    await _dispatch_until_idle(_dispatcher(sessions, slack, RecordingEmailSender()))
    assert evil.slack_calls(slack) == []
    assert all("169.254" not in str(r.url) for r in slack.requests)
    [evil_row] = await _rows(sessions, evil)
    assert evil_row.status == "dead"
    assert "hooks.slack.com" in (evil_row.last_error or "")


async def test_wrong_key_cannot_decrypt(sessions: Sessions) -> None:
    t = await _tenant(sessions, email_recipients=[])
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(t.ctx.tenant_id))
    slack = SlackMock()
    await _dispatcher(
        sessions, slack, RecordingEmailSender(), box=SecretBox("z" * 40)
    ).run_once()  # gitleaks:allow
    [row] = await _rows(sessions, t)
    assert row.status == "dead"
    assert t.slack_calls(slack) == []


async def test_disabled_rule_and_free_plan_slack_are_skipped(sessions: Sessions) -> None:
    free = await _tenant(sessions, plan=Plan.FREE)
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(free.ctx.tenant_id))
    disabled = await _tenant(sessions)
    async with sessions.begin() as s:
        await enqueue_for_analysis(s, _envelope(disabled.ctx.tenant_id))
        await alerts.update_alert_rule(
            s, disabled.ctx, disabled.project_id, disabled.rule_id, {"enabled": False}
        )
    slack, email = SlackMock(), RecordingEmailSender()
    await _dispatch_until_idle(_dispatcher(sessions, slack, email))
    free_rows = {r.channel: r for r in await _rows(sessions, free)}
    assert free_rows["slack"].status == "skipped"
    assert "free plan" in (free_rows["slack"].last_error or "")
    assert free_rows["email"].status == "sent"  # e-mail alerts are on every plan
    assert {r.status for r in await _rows(sessions, disabled)} == {"skipped"}
    assert free.slack_calls(slack) == []
    assert disabled.slack_calls(slack) == []
    assert len(free.emails(email)) == 1
    assert disabled.emails(email) == []


# ----------------------------------------------------------------------------- consumer (Kafka)
@pytest.fixture
def analysis_topic(kafka_bootstrap: str) -> str:
    from rl_worker.kafka_admin import TopicSpec, bootstrap

    name = f"analysis.results.t{uuid.uuid4().hex[:10]}"
    day_ms = 24 * 3600 * 1000
    bootstrap(
        AdminClient({"bootstrap.servers": kafka_bootstrap}),
        topics=(TopicSpec(name, 2, day_ms), TopicSpec(dlq(name), 1, day_ms)),
    )
    return name


async def test_consumer_queues_from_kafka_and_dead_letters_poison(
    sessions: Sessions, kafka_bootstrap: str, producer: Any, analysis_topic: str
) -> None:
    t = await _tenant(sessions)
    env = _envelope(t.ctx.tenant_id)
    key = f"{t.ctx.tenant_id}:{env.payload.deployment_id}"
    produce_raw(
        kafka_bootstrap,
        analysis_topic,
        [(key, encode_analysis(env)), (key, encode_analysis(env)), (key, b'{"type": "nope"}')],
    )
    consumer = AnalysisResultsConsumer(
        bootstrap_servers=kafka_bootstrap,
        sessions=sessions,
        producer=producer,
        client_id="test-notifier",
        group_id=f"rl-notifier-{uuid.uuid4().hex[:8]}",
        topic=analysis_topic,
        backoff=Backoff(base_s=0.05, cap_s=0.5, max_attempts=3),
        poll_timeout_s=0.2,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        await wait_until(lambda: consumer.stats.committed >= 3, timeout_s=60)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)
    assert consumer.enqueued == 2  # the duplicate delivery queued nothing
    assert consumer.stats.dead_lettered == 1
    assert len(await _rows(sessions, t)) == 2
    [poison] = read_topic(kafka_bootstrap, dlq(analysis_topic), expect=1)
    assert headers_of(poison)["error_type"] == b"unknown_event_type"


# ----------------------------------------------------------------------------- maintenance
async def test_maintenance_purges_by_plan_retention(sessions: Sessions) -> None:
    from rl_db.models import Deployment

    t = await _tenant(sessions, plan=Plan.FREE, service="old-svc")
    when = datetime.now(UTC) - timedelta(days=8)
    async with sessions.begin() as s:
        s.add(
            Deployment(
                id=uuid7(),
                tenant_id=t.ctx.tenant_id,
                event_id=uuid7(),
                schema_version=2,
                service="old-svc",
                environment="production",
                version="v1",
                commit_sha="abcdef1",
                occurred_at=when,
                deployed_at=when,
            )
        )
    result = await Maintenance(sessions, interval_s=3600).run_once()
    assert result.deployments_purged >= 1
    async with sessions() as s:
        left = (
            await s.execute(select(Deployment.id).where(Deployment.tenant_id == t.ctx.tenant_id))
        ).all()
    assert left == []
    async with sessions() as s:
        org = await s.get(Organization, t.ctx.tenant_id)
    assert org is not None
