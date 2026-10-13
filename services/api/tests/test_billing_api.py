"""Stripe test-mode billing: 503 without configuration, owner-only checkout and portal,
signed webhooks (signed with the Stripe SDK's own helper), idempotency by event id,
out-of-order protection and the plan changes that follow (M10)."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rl_api.testing import (
    Harness,
    UserClient,
    saas_settings,
    stripe_event,
    stripe_signature,
    unique_email,
)

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "whsec_test_" + "0" * 32  # gitleaks:allow
PRICE = "price_test_team"
WEBHOOK = "/api/v1/billing/stripe/webhook"


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_billing")


@pytest.fixture
async def h(db_url: str) -> AsyncIterator[Harness]:
    harness = Harness(
        saas_settings(
            db_url,
            stripe_secret_key="sk_test_" + "x" * 24,  # gitleaks:allow
            stripe_webhook_secret=WEBHOOK_SECRET,
            stripe_price_team=PRICE,
        )
    )
    async with harness.running():
        yield harness


async def _send(
    h: Harness, payload: bytes, *, secret: str = WEBHOOK_SECRET, ts: int | None = None
) -> Any:
    signature = stripe_signature(payload, secret, ts or int(time.time()))
    return await h.client().post(
        WEBHOOK,
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )


async def _org(h: Harness) -> tuple[UserClient, uuid.UUID]:
    owner = await h.signup(unique_email("billing"))
    return owner, await h.create_org(owner)


def _subscription(
    org_id: uuid.UUID, customer: str, sub_id: str, status: str, period_end: int
) -> dict[str, Any]:
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": False,
        "metadata": {"tenant_id": str(org_id)},
        "items": {"data": [{"price": {"id": PRICE}, "current_period_end": period_end}]},
    }


async def test_unconfigured_billing_answers_503(db_url: str) -> None:
    harness = Harness(saas_settings(db_url), gateway=None)
    async with harness.running():
        owner, org_id = await _org(harness)
        for path in (
            f"/api/v1/orgs/{org_id}/billing/checkout",
            f"/api/v1/orgs/{org_id}/billing/portal",
        ):
            resp = await owner.post(path, json={"plan": "team"})
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "billing_not_configured"
        hook = await harness.client().post(WEBHOOK, content=b"{}")
        assert hook.status_code == 503
        status = (await owner.get(f"/api/v1/orgs/{org_id}/billing")).json()
        assert status["stripe_configured"] is False
        assert status["plan"] == "free"


def test_live_stripe_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="test-mode"):
        saas_settings(
            "postgresql+asyncpg://x:y@127.0.0.1:1/z", stripe_secret_key="sk_live_abc"
        )  # gitleaks:allow


async def test_checkout_is_owner_only_and_calls_the_gateway(h: Harness) -> None:
    owner, org_id = await _org(h)
    admin = await h.add_member(owner, org_id, "admin")
    path = f"/api/v1/orgs/{org_id}/billing/checkout"
    assert (await admin.post(path, json={"plan": "team"})).status_code == 403
    resp = await owner.post(path, json={"plan": "team"})
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://checkout.stripe.test/")
    assert h.gateway is not None
    call = h.gateway.checkouts[-1]
    assert call["price_id"] == PRICE
    assert call["tenant_id"] == org_id
    assert call["customer_email"] == owner.email
    assert call["success_url"].startswith("https://app.releaselens.test/app/settings/billing")
    # No billing account yet: nothing to open in the portal.
    portal = await owner.post(f"/api/v1/orgs/{org_id}/billing/portal")
    assert portal.status_code == 409


async def test_bad_signatures_are_rejected(h: Harness) -> None:
    payload = stripe_event("customer.subscription.updated", {}, created=int(time.time()))
    assert (await _send(h, payload, secret="whsec_wrong")).status_code == 400  # gitleaks:allow
    stale = await _send(h, payload, ts=int(time.time()) - 3600)  # outside the 5-minute tolerance
    assert stale.status_code == 400
    unsigned = await h.client().post(WEBHOOK, content=payload)
    assert unsigned.status_code == 400
    assert unsigned.json()["error"]["code"] == "invalid_signature"


async def test_subscription_lifecycle_through_webhooks(h: Harness) -> None:
    owner, org_id = await _org(h)
    customer, sub_id = f"cus_{uuid.uuid4().hex[:12]}", f"sub_{uuid.uuid4().hex[:12]}"
    now = int(time.time())

    completed = stripe_event(
        "checkout.session.completed",
        {
            "id": "cs_test_1",
            "object": "checkout.session",
            "mode": "subscription",
            "client_reference_id": str(org_id),
            "customer": customer,
            "subscription": sub_id,
            "metadata": {"tenant_id": str(org_id)},
        },
        created=now,
    )
    first = await _send(h, completed)
    assert first.json() == {"status": "processed"}
    assert (await _send(h, completed)).json() == {"status": "duplicate"}  # Stripe retry
    billing = (await owner.get(f"/api/v1/orgs/{org_id}/billing")).json()
    assert billing["plan"] == "team"
    assert billing["limits"]["slack_alerts"] is True
    assert billing["subscription"]["status"] == "active"

    period_end = now + 30 * 86400
    updated = stripe_event(
        "customer.subscription.updated",
        _subscription(org_id, customer, sub_id, "past_due", period_end),
        created=now + 10,
    )
    assert (await _send(h, updated)).json()["status"] == "processed"
    failed = stripe_event(
        "invoice.payment_failed",
        {
            "id": "in_test_1",
            "object": "invoice",
            "customer": customer,
            "attempt_count": 1,
            "parent": {"subscription_details": {"subscription": sub_id}},
        },
        created=now + 11,
    )
    assert (await _send(h, failed)).json()["status"] == "processed"
    billing = (await owner.get(f"/api/v1/orgs/{org_id}/billing")).json()
    assert billing["plan"] == "team"  # past_due keeps access while Stripe retries
    assert billing["subscription"]["status"] == "past_due"
    assert billing["subscription"]["payment_failed_at"] is not None
    assert billing["subscription"]["current_period_end"] is not None

    # The portal now has a customer to open.
    portal = await owner.post(f"/api/v1/orgs/{org_id}/billing/portal")
    assert portal.status_code == 200
    assert h.gateway is not None
    assert h.gateway.portals[-1]["customer_id"] == customer
    assert (await owner.post(f"/api/v1/orgs/{org_id}/billing/checkout", json={})).status_code == 409

    deleted = stripe_event(
        "customer.subscription.deleted",
        _subscription(org_id, customer, sub_id, "canceled", period_end),
        created=now + 20,
    )
    assert (await _send(h, deleted)).json()["status"] == "processed"
    # A stale "active" update delivered after the deletion must not resurrect the plan.
    stale = stripe_event(
        "customer.subscription.updated",
        _subscription(org_id, customer, sub_id, "active", period_end),
        created=now + 15,
    )
    assert (await _send(h, stale)).json()["status"] == "processed"
    billing = (await owner.get(f"/api/v1/orgs/{org_id}/billing")).json()
    assert billing["plan"] == "free"
    assert billing["subscription"]["status"] == "canceled"

    audit = (
        await owner.get(f"/api/v1/orgs/{org_id}/audit-log", params={"target_type": "billing"})
    ).json()
    actions = [e["action"] for e in audit["items"]]
    assert "billing.payment_failed" in actions
    assert actions.count("billing.subscription_changed") >= 3
    assert all(e["actor_type"] == "system" for e in audit["items"])


async def test_events_for_unknown_tenants_or_types_are_acknowledged_and_ignored(h: Harness) -> None:
    now = int(time.time())
    unknown_tenant = stripe_event(
        "checkout.session.completed",
        {"mode": "subscription", "client_reference_id": str(uuid.uuid4()), "customer": "cus_x"},
        created=now,
    )
    assert (await _send(h, unknown_tenant)).json() == {"status": "ignored"}
    other = stripe_event("charge.refunded", {"id": "ch_1"}, created=now)
    assert (await _send(h, other)).json() == {"status": "ignored"}


async def test_public_plan_list_matches_the_pricing_page(h: Harness) -> None:
    plans = {p["plan"]: p for p in (await h.client().get("/api/v1/plans")).json()}
    assert (
        plans["free"]["max_projects"],
        plans["free"]["max_services"],
        plans["free"]["retention_days"],
    ) == (1, 3, 7)
    assert (
        plans["team"]["max_projects"],
        plans["team"]["max_services"],
        plans["team"]["retention_days"],
    ) == (10, 50, 30)
    assert plans["team"]["price_usd_month"] == 99
    assert plans["team"]["slack_alerts"] is True
    assert plans["team"]["github_checks"] is True
    assert plans["enterprise"]["self_serve"] is False
