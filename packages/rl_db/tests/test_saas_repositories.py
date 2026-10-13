"""M10 persistence against a real, migrated Postgres 16: audit immutability, token buckets,
single-use tokens, notification idempotency and dispatch claims, retention, billing order."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin import accounts, billing, notifications, ratelimit, retention
from rl_db.admin.identity import create_organization
from rl_db.models import (
    AuditLogEntry,
    Deployment,
    NotificationLog,
    Organization,
    Plan,
    TokenPurpose,
)
from rl_db.repositories import alerts, audit
from rl_db.repositories import projects as catalog

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_url, pool_size=8)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    return create_session_factory(engine)


async def _tenant(
    sessions: Sessions, *, plan: Plan = Plan.FREE, demo: bool = False
) -> TenantContext:
    async with sessions.begin() as s:
        org = await create_organization(
            s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:10]}", is_demo=demo
        )
        org.plan = plan
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)


# ----------------------------------------------------------------------------- audit log
async def test_audit_log_is_append_only_at_the_database_level(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    async with sessions.begin() as s:
        entry_id = await audit.record_audit(
            s,
            ctx,
            action="api_key.created",
            actor=audit.AuditActor(actor_type="user", user_id=uuid7(), email="a@example.com"),
            target_type="api_key",
            target_id="k1",
            request=audit.AuditRequestInfo(ip="203.0.113.7", user_agent="pytest"),
            details={"scopes": ["read"]},
        )
    for sql in (
        "UPDATE audit_log SET action = 'forged' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with sessions.begin() as s:
                await s.execute(text(sql), {"id": entry_id})
    async with sessions() as s:
        row = await s.get(AuditLogEntry, entry_id)
    assert row is not None
    assert (row.action, str(row.ip), row.details) == (
        "api_key.created",
        "203.0.113.7",
        {"scopes": ["read"]},
    )


async def test_audit_listing_is_tenant_scoped_and_keyset_paginated(sessions: Sessions) -> None:
    a, b = await _tenant(sessions), await _tenant(sessions)
    actor = audit.SYSTEM_ACTOR
    async with sessions.begin() as s:
        for i in range(5):
            await audit.record_audit(s, a, action="settings.changed", actor=actor, target_id=str(i))
        await audit.record_audit(s, b, action="settings.changed", actor=actor)
    async with sessions() as s:
        first = await audit.list_audit(s, a, filters=audit.AuditFilter(), limit=3)
        cursor = audit.AuditCursor(created_at=first[-1].created_at, id=first[-1].id)
        rest = await audit.list_audit(s, a, filters=audit.AuditFilter(), limit=3, after=cursor)
        only_b = await audit.list_audit(s, b, filters=audit.AuditFilter(), limit=10)
    assert len(first) == 3
    assert len(rest) == 2
    assert {e.tenant_id for e in first + rest} == {a.tenant_id}
    assert len({e.id for e in first + rest}) == 5
    assert [e.tenant_id for e in only_b] == [b.tenant_id]


# ----------------------------------------------------------------------------- rate limits
async def test_token_bucket_allows_capacity_then_refuses_with_retry_after(
    sessions: Sessions,
) -> None:
    key = f"test:{uuid.uuid4().hex}"
    decisions = []
    for _ in range(4):
        async with sessions.begin() as s:
            decisions.append(await ratelimit.take_token(s, key, capacity=3, refill_per_s=0.01))
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[0].remaining == 2
    assert 1 <= decisions[3].retry_after_s <= 100


async def test_token_bucket_is_exact_under_concurrency(sessions: Sessions) -> None:
    key = f"test:{uuid.uuid4().hex}"

    async def take() -> bool:
        async with sessions.begin() as s:
            return (await ratelimit.take_token(s, key, capacity=5, refill_per_s=0.001)).allowed

    results = await asyncio.gather(*(take() for _ in range(20)))
    assert sum(results) == 5


async def test_token_bucket_refills(sessions: Sessions) -> None:
    key = f"test:{uuid.uuid4().hex}"
    async with sessions.begin() as s:
        assert (await ratelimit.take_token(s, key, capacity=1, refill_per_s=20)).allowed
        assert not (await ratelimit.take_token(s, key, capacity=1, refill_per_s=20)).allowed
    await asyncio.sleep(0.2)  # 20 tokens/s refills one token in 50 ms
    async with sessions.begin() as s:
        assert (await ratelimit.take_token(s, key, capacity=1, refill_per_s=20)).allowed


# ----------------------------------------------------------------------------- one-time tokens
async def test_user_tokens_are_single_use_expiring_and_purpose_bound(sessions: Sessions) -> None:
    now = datetime.now(UTC)
    async with sessions.begin() as s:
        user = await accounts.create_user(
            s, email=f"U{uuid.uuid4().hex[:6]}@Example.com", password_hash=None
        )
        await accounts.issue_user_token(
            s,
            user_id=user.id,
            purpose=TokenPurpose.VERIFY_EMAIL,
            token_hash="a" * 64,
            expires_at=now + timedelta(hours=24),
            now=now,
        )
        await accounts.issue_user_token(
            s,
            user_id=user.id,
            purpose=TokenPurpose.RESET_PASSWORD,
            token_hash="b" * 64,
            expires_at=now - timedelta(seconds=1),
            now=now,
        )
    assert user.email == user.email.lower()
    async with sessions.begin() as s:
        wrong_purpose = await accounts.consume_user_token(
            s, purpose=TokenPurpose.RESET_PASSWORD, token_hash="a" * 64, now=now
        )
        expired = await accounts.consume_user_token(
            s, purpose=TokenPurpose.RESET_PASSWORD, token_hash="b" * 64, now=now
        )
        first = await accounts.consume_user_token(
            s, purpose=TokenPurpose.VERIFY_EMAIL, token_hash="a" * 64, now=now
        )
        second = await accounts.consume_user_token(
            s, purpose=TokenPurpose.VERIFY_EMAIL, token_hash="a" * 64, now=now
        )
    assert (wrong_purpose, expired, first, second) == (None, None, user.id, None)


async def test_issuing_a_token_retires_the_previous_one(sessions: Sessions) -> None:
    now = datetime.now(UTC)
    async with sessions.begin() as s:
        user = await accounts.create_user(
            s, email=f"r{uuid.uuid4().hex[:6]}@x.io", password_hash=None
        )
        for h in ("c" * 64, "d" * 64):
            await accounts.issue_user_token(
                s,
                user_id=user.id,
                purpose=TokenPurpose.RESET_PASSWORD,
                token_hash=h,
                expires_at=now + timedelta(hours=1),
                now=now,
            )
    async with sessions.begin() as s:
        old = await accounts.consume_user_token(
            s, purpose=TokenPurpose.RESET_PASSWORD, token_hash="c" * 64, now=now
        )
        new = await accounts.consume_user_token(
            s, purpose=TokenPurpose.RESET_PASSWORD, token_hash="d" * 64, now=now
        )
    assert (old, new) == (None, user.id)


# ----------------------------------------------------------------------------- catalog
async def test_composite_foreign_key_keeps_services_in_their_projects_tenant(
    sessions: Sessions,
) -> None:
    a, b = await _tenant(sessions), await _tenant(sessions)
    async with sessions.begin() as s:
        project = await catalog.create_project(s, a, name="Web", slug="web")
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            await catalog.create_service(s, b, project_id=project.id, name="api")
    async with sessions() as s:
        assert await catalog.get_project(s, b, project.id) is None


# ----------------------------------------------------------------------------- notifications
async def _rule(
    sessions: Sessions, ctx: TenantContext, *, slack: bool = True, email: bool = True, **kw: object
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions.begin() as s:
        project = await catalog.create_project(s, ctx, name="P", slug=f"p{uuid.uuid4().hex[:8]}")
        rule = await alerts.create_alert_rule(
            s,
            ctx,
            project_id=project.id,
            values={
                "name": "on-call",
                "slack_webhook_ciphertext": "v1.x.y" if slack else None,
                "email_recipients": ["oncall@example.com"] if email else [],
                **kw,
            },
        )
    return project.id, rule.id


async def test_enqueue_is_idempotent_and_band_gated(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    project_id, _ = await _rule(sessions, ctx, regression_min_band="CRITICAL")
    deployment = uuid7()

    async def enqueue(band: str) -> alerts.EnqueueResult:
        async with sessions.begin() as s:
            return await alerts.enqueue_notifications(
                s,
                ctx,
                project_id=project_id,
                kind="regression",
                subject_id=deployment,
                band=band,
                payload={"band": band},
            )

    assert (await enqueue("HIGH")).created == 0  # below this rule's minimum
    assert (await enqueue("CRITICAL")).created == 2  # slack + email
    assert (await enqueue("CRITICAL")).created == 0  # redelivery: nothing new
    async with sessions() as s:
        n = await s.scalar(select(func.count()).where(NotificationLog.tenant_id == ctx.tenant_id))
    assert n == 2


async def test_disabled_rules_and_risk_trigger_flags_are_respected(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    project_id, _ = await _rule(sessions, ctx, enabled=False)
    async with sessions.begin() as s:
        disabled = await alerts.enqueue_notifications(
            s,
            ctx,
            project_id=project_id,
            kind="regression",
            subject_id=uuid7(),
            band="CRITICAL",
            payload={},
        )
        risk_off = await alerts.enqueue_notifications(
            s,
            ctx,
            project_id=project_id,
            kind="risk",
            subject_id=uuid7(),
            band="CRITICAL",
            payload={},
        )
    assert (disabled.created, risk_off.created) == (0, 0)


async def test_dispatch_claims_never_overlap_and_failures_back_off_then_die(
    sessions: Sessions,
) -> None:
    ctx = await _tenant(sessions)
    project_id, _ = await _rule(sessions, ctx, email=False)
    async with sessions.begin() as s:
        await alerts.enqueue_notifications(
            s,
            ctx,
            project_id=project_id,
            kind="regression",
            subject_id=uuid7(),
            band="HIGH",
            payload={},
        )
        await s.execute(
            NotificationLog.__table__.update()
            .where(NotificationLog.tenant_id == ctx.tenant_id)
            .values(max_attempts=2)
        )

    async def claim(worker: str) -> list[notifications.ClaimedNotification]:
        async with sessions.begin() as s:
            return await notifications.claim_due_notifications(
                s, worker_id=worker, limit=10, lease=timedelta(minutes=5)
            )

    first, second = await asyncio.gather(claim("w1"), claim("w2"))
    mine = [n for n in first + second if n.tenant_id == ctx.tenant_id]
    assert len(mine) == 1
    claimed = mine[0]
    worker = "w1" if claimed in first else "w2"

    async with sessions.begin() as s:
        status = await notifications.mark_failed(
            s, claimed.id, worker, error="503", retry_delay=timedelta(0), permanent=False
        )
    assert status == "failed"
    again = [n for n in await claim("w3") if n.id == claimed.id]
    assert again
    assert again[0].attempts == 2
    async with sessions.begin() as s:
        status = await notifications.mark_failed(
            s, claimed.id, "w3", error="503 again", retry_delay=timedelta(0), permanent=False
        )
    assert status == "dead"  # attempts used up
    assert not [n for n in await claim("w4") if n.id == claimed.id]


# ----------------------------------------------------------------------------- retention
async def test_retention_purges_by_plan_and_spares_demo_orgs(sessions: Sessions) -> None:
    free = await _tenant(sessions, plan=Plan.FREE)
    team = await _tenant(sessions, plan=Plan.TEAM)
    demo = await _tenant(sessions, plan=Plan.FREE, demo=True)
    now = datetime.now(UTC)
    ages = {"d3": 3, "d10": 10, "d40": 40}

    async with sessions.begin() as s:
        for ctx in (free, team, demo):
            for label, days in ages.items():
                when = now - timedelta(days=days)
                s.add(
                    Deployment(
                        id=uuid7(),
                        tenant_id=ctx.tenant_id,
                        event_id=uuid7(),
                        schema_version=2,
                        service="svc",
                        environment="production",
                        version=label,
                        commit_sha="abcdef1",
                        occurred_at=when,
                        deployed_at=when,
                    )
                )
    while True:
        async with sessions.begin() as s:
            if await retention.purge_expired_deployments(s, batch_size=2) == 0:
                break

    async def versions(ctx: TenantContext) -> set[str]:
        async with sessions() as s:
            rows = await s.scalars(
                select(Deployment.version).where(Deployment.tenant_id == ctx.tenant_id)
            )
            return set(rows)

    assert await versions(free) == {"d3"}  # 7 days
    assert await versions(team) == {"d3", "d10"}  # 30 days
    assert await versions(demo) == set(ages)  # exempt


# ----------------------------------------------------------------------------- billing
async def test_subscription_state_ignores_out_of_order_events(sessions: Sessions) -> None:
    ctx = await _tenant(sessions)
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    customer = f"cus_{uuid.uuid4().hex[:12]}"

    def state(status: str, plan: Plan, at: datetime) -> billing.SubscriptionState:
        return billing.SubscriptionState(
            tenant_id=ctx.tenant_id,
            customer_id=customer,
            subscription_id=f"sub_{customer}",
            status=status,
            plan=plan,
            price_id="price_team",
            current_period_end=None,
            cancel_at_period_end=False,
            event_at=at,
        )

    async with sessions.begin() as s:
        assert await billing.apply_subscription_state(s, state("active", Plan.TEAM, t0))
        # A newer cancellation, then a stale "active" that arrives late.
        assert await billing.apply_subscription_state(
            s, state("canceled", Plan.FREE, t0 + timedelta(hours=1))
        )
        assert not await billing.apply_subscription_state(
            s, state("active", Plan.TEAM, t0 + timedelta(minutes=30))
        )
    async with sessions() as s:
        org = await s.get(Organization, ctx.tenant_id)
        sub = await billing.find_subscription(s, customer_id=customer)
    assert org is not None
    assert org.plan == Plan.FREE
    assert sub is not None
    assert sub.status == "canceled"


async def test_stripe_event_ids_are_recorded_once(sessions: Sessions) -> None:
    event_id = f"evt_{uuid.uuid4().hex}"
    at = datetime.now(UTC)
    async with sessions.begin() as s:
        assert await billing.record_stripe_event(
            s, event_id=event_id, event_type="x", created_at=at
        )
    async with sessions.begin() as s:
        assert not await billing.record_stripe_event(
            s, event_id=event_id, event_type="x", created_at=at
        )
