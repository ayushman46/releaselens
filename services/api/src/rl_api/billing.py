"""Stripe billing in **test mode** (docs/billing/BILLING.md; BLOCKERS.md B2).

* :class:`BillingGateway` is the seam: :class:`StripeGateway` talks to Stripe through the
  official SDK (``stripe==15.6.1``, API version ``2026-08-26.dahlia``); tests inject a fake.
* :func:`verify_webhook` checks ``Stripe-Signature`` with the SDK's verifier (HMAC-SHA256
  over ``"{t}.{payload}"``, 5-minute tolerance) before anything is parsed.
* :func:`apply_event` turns the four handled event types into ``subscriptions`` rows and
  ``organizations.plan``. It runs in the webhook's single transaction together with the
  event-id record, so a crash mid-way leaves no trace and Stripe's retry redoes it.

Plan mapping: an active, trialing or past-due subscription on ``RL_STRIPE_PRICE_TEAM`` is
the Team plan (past-due keeps access while Stripe retries the card); anything else
(canceled, unpaid, incomplete…) falls back to Free.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import stripe
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.tenancy import Principal, TenantContext
from rl_db.admin import billing as billing_admin
from rl_db.models import Plan
from rl_db.repositories.audit import SYSTEM_ACTOR, record_audit

log = logging.getLogger("rl_api.billing")

WEBHOOK_TOLERANCE_S = 300
ENTITLED_STATUSES = frozenset({"active", "trialing", "past_due"})
HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)


class BillingError(Exception):
    pass


class BillingGateway(Protocol):
    async def create_checkout_session(
        self,
        *,
        price_id: str,
        tenant_id: uuid.UUID,
        customer_id: str | None,
        customer_email: str | None,
        success_url: str,
        cancel_url: str,
    ) -> str: ...

    async def create_portal_session(self, *, customer_id: str, return_url: str) -> str: ...


class StripeGateway:
    def __init__(self, secret_key: str) -> None:
        self._client = stripe.StripeClient(secret_key, http_client=stripe.HTTPXClient())

    async def create_checkout_session(
        self,
        *,
        price_id: str,
        tenant_id: uuid.UUID,
        customer_id: str | None,
        customer_email: str | None,
        success_url: str,
        cancel_url: str,
    ) -> str:
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            # How the webhook finds the tenant again (both are echoed back by Stripe).
            "client_reference_id": str(tenant_id),
            "metadata": {"tenant_id": str(tenant_id)},
            "subscription_data": {"metadata": {"tenant_id": str(tenant_id)}},
        }
        if customer_id:
            params["customer"] = customer_id
        elif customer_email:
            params["customer_email"] = customer_email
        try:
            session = await self._client.v1.checkout.sessions.create_async(params=params)  # type: ignore[arg-type]
        except stripe.StripeError as exc:
            raise BillingError(f"stripe checkout failed: {type(exc).__name__}") from exc
        if not session.url:
            raise BillingError("stripe returned a checkout session without a URL")
        return str(session.url)

    async def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        try:
            portal = await self._client.v1.billing_portal.sessions.create_async(
                params={"customer": customer_id, "return_url": return_url}
            )
        except stripe.StripeError as exc:
            raise BillingError(f"stripe portal failed: {type(exc).__name__}") from exc
        return str(portal.url)


class InvalidSignatureError(Exception):
    pass


def verify_webhook(payload: bytes, signature: str | None, secret: str) -> dict[str, Any]:
    """Verify ``Stripe-Signature`` and return the parsed event (raises on any problem)."""
    if not signature:
        raise InvalidSignatureError("missing Stripe-Signature header")
    try:
        text = payload.decode("utf-8")
        stripe.WebhookSignature.verify_header(text, signature, secret, WEBHOOK_TOLERANCE_S)
        event = json.loads(text)
    except (UnicodeDecodeError, ValueError, stripe.SignatureVerificationError) as exc:
        raise InvalidSignatureError(str(exc)[:200]) from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
        raise InvalidSignatureError("not a Stripe event")
    return event


# ----------------------------------------------------------------------------- event handling


def _id(value: Any) -> str | None:
    """A Stripe reference: either an id string or an expanded object with an ``id``."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return str(value["id"])
    return None


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if isinstance(value, int | float) else None


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _first_item(subscription: dict[str, Any]) -> dict[str, Any]:
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}


@dataclass(frozen=True, slots=True)
class EventOutcome:
    status: str  # processed | duplicate | ignored
    tenant_id: uuid.UUID | None = None


class StripeEventHandler:
    def __init__(self, *, team_price_id: str | None) -> None:
        self._team_price_id = team_price_id

    def plan_for(self, status: str, price_id: str | None, metadata: dict[str, Any]) -> Plan:
        if status not in ENTITLED_STATUSES:
            return Plan.FREE
        if metadata.get("rl_plan") == "enterprise":
            return Plan.ENTERPRISE  # set by hand in Stripe for contract customers
        if self._team_price_id and price_id and price_id != self._team_price_id:
            log.warning(
                "subscription on an unknown price; treated as Team", extra={"price": price_id}
            )
        return Plan.TEAM

    async def apply(self, session: AsyncSession, event: dict[str, Any]) -> EventOutcome:
        event_id = str(event["id"])
        event_type = str(event.get("type", ""))
        created = _ts(event.get("created")) or datetime.now(UTC)
        fresh = await billing_admin.record_stripe_event(
            session, event_id=event_id, event_type=event_type, created_at=created
        )
        if not fresh:
            return EventOutcome("duplicate")
        if event_type not in HANDLED_EVENTS:
            return EventOutcome("ignored")
        data = event.get("data")
        obj = data.get("object") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            log.warning("stripe event without data.object", extra={"event_id": event_id})
            return EventOutcome("ignored")

        if event_type == "checkout.session.completed":
            tenant_id = await self._checkout_completed(session, event_id, obj, created)
        elif event_type == "invoice.payment_failed":
            tenant_id = await self._payment_failed(session, event_id, obj, created)
        else:
            tenant_id = await self._subscription_changed(
                session, event_id, obj, created, deleted=event_type.endswith(".deleted")
            )
        if tenant_id is None:
            return EventOutcome("ignored")
        await billing_admin.tag_stripe_event(session, event_id, tenant_id)
        return EventOutcome("processed", tenant_id)

    async def _known_tenant(self, session: AsyncSession, value: Any) -> uuid.UUID | None:
        tenant_id = _uuid(value)
        if tenant_id is None or not await billing_admin.organization_exists(session, tenant_id):
            return None
        return tenant_id

    async def _audit(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        action: str,
        details: dict[str, Any],
    ) -> None:
        ctx = TenantContext(tenant_id=tenant_id, principal=Principal.SYSTEM)
        await record_audit(
            session, ctx, action=action, actor=SYSTEM_ACTOR, target_type="billing", details=details
        )

    async def _checkout_completed(
        self, session: AsyncSession, event_id: str, obj: dict[str, Any], created: datetime
    ) -> uuid.UUID | None:
        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        tenant_id = await self._known_tenant(
            session, obj.get("client_reference_id") or (metadata or {}).get("tenant_id")
        )
        customer = _id(obj.get("customer"))
        if tenant_id is None or customer is None or obj.get("mode") != "subscription":
            log.warning("checkout.session.completed not applicable", extra={"event_id": event_id})
            return None
        state = billing_admin.SubscriptionState(
            tenant_id=tenant_id,
            customer_id=customer,
            subscription_id=_id(obj.get("subscription")),
            status="active",
            plan=Plan.TEAM,
            price_id=self._team_price_id,
            current_period_end=None,
            cancel_at_period_end=False,
            event_at=created,
        )
        if await billing_admin.apply_subscription_state(session, state):
            await self._audit(
                session,
                tenant_id,
                "billing.subscription_changed",
                {"event_id": event_id, "status": "active", "plan": Plan.TEAM.value},
            )
        return tenant_id

    async def _subscription_changed(
        self,
        session: AsyncSession,
        event_id: str,
        obj: dict[str, Any],
        created: datetime,
        *,
        deleted: bool,
    ) -> uuid.UUID | None:
        subscription_id = _id(obj.get("id"))
        customer = _id(obj.get("customer"))
        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        existing = await billing_admin.find_subscription(
            session, customer_id=customer, subscription_id=subscription_id
        )
        tenant_id = (
            existing.tenant_id
            if existing is not None
            else await self._known_tenant(session, (metadata or {}).get("tenant_id"))
        )
        if tenant_id is None or customer is None:
            log.warning("subscription event for an unknown tenant", extra={"event_id": event_id})
            return None
        status = "canceled" if deleted else str(obj.get("status", "incomplete"))
        item = _first_item(obj)
        price = item.get("price")
        price_id = _id(price)
        plan = self.plan_for(status, price_id, metadata or {})
        state = billing_admin.SubscriptionState(
            tenant_id=tenant_id,
            customer_id=customer,
            subscription_id=subscription_id,
            status=status,
            plan=plan,
            price_id=price_id,
            # API 2025-03-31+ moved the period to the subscription items.
            current_period_end=_ts(item.get("current_period_end"))
            or _ts(obj.get("current_period_end")),
            cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
            event_at=created,
        )
        if await billing_admin.apply_subscription_state(session, state):
            await self._audit(
                session,
                tenant_id,
                "billing.subscription_changed",
                {"event_id": event_id, "status": status, "plan": plan.value},
            )
        return tenant_id

    async def _payment_failed(
        self, session: AsyncSession, event_id: str, obj: dict[str, Any], created: datetime
    ) -> uuid.UUID | None:
        parent = obj.get("parent") if isinstance(obj.get("parent"), dict) else {}
        details = (parent or {}).get("subscription_details")
        subscription_id = _id(obj.get("subscription")) or (
            _id(details.get("subscription")) if isinstance(details, dict) else None
        )
        existing = await billing_admin.find_subscription(
            session, customer_id=_id(obj.get("customer")), subscription_id=subscription_id
        )
        if existing is None:
            log.warning("payment failure for an unknown customer", extra={"event_id": event_id})
            return None
        await billing_admin.mark_payment_failed(session, existing, at=created)
        await self._audit(
            session,
            existing.tenant_id,
            "billing.payment_failed",
            {"event_id": event_id, "attempt_count": obj.get("attempt_count")},
        )
        return existing.tenant_id
