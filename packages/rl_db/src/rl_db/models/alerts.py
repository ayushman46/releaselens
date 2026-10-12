"""Alert rules and the notification log (M10).

An **alert rule** belongs to a project and says which events notify whom:

* triggers: post-deploy regression with severity band ≥ ``regression_min_band`` and/or
  pre-deploy risk band ≥ ``risk_min_band`` (both ``HIGH`` or ``CRITICAL``);
* channels: one Slack incoming webhook (stored **encrypted**, :mod:`rl_db.secretbox`; only a
  short hint is kept in clear) and up to 20 e-mail recipients.

The **notification log** has one row per (rule, subject, kind, channel). Its unique key is
what makes notifications idempotent: a redelivered ``analysis.results`` message, or a
second analysis of the same deployment, finds the row already there and sends nothing.
The row then moves through::

    pending ─claim─▶ sending ─ok─▶ sent
                        │ transient error, attempts left ─▶ failed (next_attempt_at = backoff)
                        │ permanent error or attempts used ─▶ dead
    pending ─▶ skipped (rule disabled/deleted or channel not allowed on the plan)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

BANDS = ("HIGH", "CRITICAL")
MAX_EMAIL_RECIPIENTS = 20
MAX_RULE_NAME_LEN = 120
DEFAULT_MAX_NOTIFICATION_ATTEMPTS = 6


class NotificationKind:
    REGRESSION = "regression"
    RISK = "risk"


class NotificationChannel:
    SLACK = "slack"
    EMAIL = "email"


class NotificationStatus:
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"
    SKIPPED = "skipped"


class AlertRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "alert_rules"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_RULE_NAME_LEN), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    on_regression: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    regression_min_band: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="HIGH"
    )
    on_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    risk_min_band: Mapped[str] = mapped_column(String(16), nullable=False, server_default="HIGH")
    slack_webhook_ciphertext: Mapped[str | None] = mapped_column(Text)
    slack_webhook_hint: Mapped[str | None] = mapped_column(String(80))
    email_recipients: Mapped[list[str]] = mapped_column(
        ARRAY(String(320)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index("ix_alert_rules_tenant_id_project_id", "tenant_id", "project_id"),
        CheckConstraint("regression_min_band IN ('HIGH', 'CRITICAL')", name="regression_band"),
        CheckConstraint("risk_min_band IN ('HIGH', 'CRITICAL')", name="risk_band"),
        CheckConstraint(
            f"cardinality(email_recipients) <= {MAX_EMAIL_RECIPIENTS}", name="recipients_max"
        ),
    )


class NotificationLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notification_log"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Kept (as NULL) when the rule is deleted, so the delivery history survives.
    alert_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the notification is about: the deployment id for ``regression``, the risk
    # assessment id for ``risk``.
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=str(DEFAULT_MAX_NOTIFICATION_ATTEMPTS)
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # What to say (the analysis summary); never a secret such as the webhook URL.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The idempotency key (one notification per rule, subject, kind and channel).
        UniqueConstraint(
            "tenant_id",
            "alert_rule_id",
            "subject_id",
            "kind",
            "channel",
            name="uq_notification_log_idempotency",  # the generated name exceeds 63 chars
        ),
        Index("ix_notification_log_tenant_id_created_at", "tenant_id", "created_at"),
        # The dispatcher's poll spans all tenants by design (like analysis_jobs).
        Index("ix_notification_log_status_next_attempt_at", "status", "next_attempt_at"),
        CheckConstraint("kind IN ('regression', 'risk')", name="kind_known"),
        CheckConstraint("channel IN ('slack', 'email')", name="channel_known"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'dead', 'skipped')",
            name="status_known",
        ),
    )
