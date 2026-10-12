"""Billing state mirrored from Stripe, usage counters and rate-limit buckets (M10).

``organizations.plan`` is what the limits read. Stripe is the source of truth for the
subscription; the webhook handler copies its state into ``subscriptions`` and sets the plan.
``stripe_events`` makes webhook processing idempotent by event id.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    Enum,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from rl_db.models.identity import Plan


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The organization's Stripe customer and (current) subscription. One row per org."""

    __tablename__ = "subscriptions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    stripe_customer_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    # Stripe's subscription status (active, trialing, past_due, canceled, unpaid, …).
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    plan: Mapped[Plan] = mapped_column(
        Enum(Plan, name="plan", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    price_id: Mapped[str | None] = mapped_column(String(255))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    payment_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ``created`` of the last Stripe event applied. Stripe does not guarantee ordering, so
    # an older event arriving late must not overwrite newer state.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StripeEvent(Base):
    """Every processed webhook event id (idempotency; Stripe retries deliveries)."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    stripe_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageCounter(Base):
    """Per-tenant, per-month usage (e.g. ``deployments`` accepted)."""

    __tablename__ = "usage_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RateLimitBucket(Base):
    """A token bucket, updated with one UPSERT per request (proposed ADR "rate limiting
    without Redis"). ``allowed`` is the decision of the last request."""

    __tablename__ = "rate_limit_buckets"

    bucket_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    tokens: Mapped[float] = mapped_column(Double, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_rate_limit_buckets_updated_at", "updated_at"),)
