"""Stripe webhook bookkeeping (cross-tenant: an event names a Stripe customer, and that is
how the tenant is found). Every function runs inside the webhook's single transaction, so
the event-id record and the state change commit together or not at all."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_db.models.billing import StripeEvent, Subscription
from rl_db.models.identity import Organization, Plan


async def record_stripe_event(
    session: AsyncSession, *, event_id: str, event_type: str, created_at: datetime
) -> bool:
    """Remember an event id. False if it was already processed (Stripe redelivered it)."""
    stmt = (
        pg_insert(StripeEvent)
        .values(id=event_id, type=event_type, stripe_created_at=created_at)
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(StripeEvent.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def tag_stripe_event(session: AsyncSession, event_id: str, tenant_id: uuid.UUID) -> None:
    await session.execute(
        update(StripeEvent).where(StripeEvent.id == event_id).values(tenant_id=tenant_id)
    )


async def organization_exists(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    stmt = select(Organization.id).where(Organization.id == tenant_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def find_subscription(
    session: AsyncSession,
    *,
    customer_id: str | None = None,
    subscription_id: str | None = None,
) -> Subscription | None:
    """The subscription row for a Stripe customer or subscription id (locked for update)."""
    if customer_id is None and subscription_id is None:
        return None
    stmt = select(Subscription).with_for_update()
    if subscription_id is not None:
        found = (
            await session.execute(
                stmt.where(Subscription.stripe_subscription_id == subscription_id)
            )
        ).scalar_one_or_none()
        if found is not None or customer_id is None:
            return found
    return (
        await session.execute(stmt.where(Subscription.stripe_customer_id == customer_id))
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    tenant_id: uuid.UUID
    customer_id: str
    subscription_id: str | None
    status: str
    plan: Plan
    price_id: str | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    event_at: datetime


async def apply_subscription_state(session: AsyncSession, state: SubscriptionState) -> bool:
    """Upsert the tenant's subscription and set ``organizations.plan``, unless a newer event
    was already applied (Stripe does not guarantee delivery order). Returns True if applied.
    """
    stmt = select(Subscription).where(Subscription.tenant_id == state.tenant_id).with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None and row.last_event_at is not None and row.last_event_at > state.event_at:
        return False
    if row is None:
        session.add(
            Subscription(
                id=uuid7(),
                tenant_id=state.tenant_id,
                stripe_customer_id=state.customer_id,
                stripe_subscription_id=state.subscription_id,
                status=state.status,
                plan=state.plan,
                price_id=state.price_id,
                current_period_end=state.current_period_end,
                cancel_at_period_end=state.cancel_at_period_end,
                last_event_at=state.event_at,
            )
        )
    else:
        row.stripe_customer_id = state.customer_id
        if state.subscription_id is not None:
            row.stripe_subscription_id = state.subscription_id
        row.status = state.status
        row.plan = state.plan
        row.price_id = state.price_id or row.price_id
        row.current_period_end = state.current_period_end or row.current_period_end
        row.cancel_at_period_end = state.cancel_at_period_end
        row.last_event_at = state.event_at
        if state.status in ("active", "trialing"):
            row.payment_failed_at = None
    await session.execute(
        update(Organization).where(Organization.id == state.tenant_id).values(plan=state.plan)
    )
    await session.flush()
    return True


async def mark_payment_failed(
    session: AsyncSession, subscription: Subscription, *, at: datetime
) -> None:
    subscription.payment_failed_at = at
    await session.flush()
