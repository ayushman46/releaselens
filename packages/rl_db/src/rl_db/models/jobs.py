"""Delayed, retryable analysis jobs (ADR-0011): a Postgres table polled with SKIP LOCKED.

Each recorded deployment gets one job per :class:`JobKind`. Its ``run_at`` is the deploy time
plus the window the analysis needs: early looks at +5 and +10 minutes, and a final verdict
when the post-deploy window closes.

Lifecycle::

    pending ──claim──▶ running ──ok──▶ succeeded
       ▲                  │
       └──── failed ◀─────┤ error, attempts < max_attempts (run_at pushed back)
                          └ error, attempts = max_attempts ──▶ dead

``attempts`` is incremented when a job is *claimed*, not when it fails. A worker that dies
mid-job still uses up an attempt, so a job that crashes its worker cannot loop forever.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JobKind(StrEnum):
    EARLY_5M = "early_5m"
    EARLY_10M = "early_10m"
    FINAL = "final"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # retryable; run_at holds the next attempt time
    DEAD = "dead"  # gave up after max_attempts


DEFAULT_MAX_ATTEMPTS = 5


class AnalysisJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_jobs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deployment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[JobKind] = mapped_column(
        Enum(JobKind, name="analysis_job_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="analysis_job_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=JobStatus.PENDING.value,
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=str(DEFAULT_MAX_ATTEMPTS)
    )
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Composite FK: a job can only point at a deployment of its own tenant.
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
        ),
        # One job per kind per deployment, so scheduling is idempotent. Leading with
        # tenant_id makes this index also serve "jobs of deployment D" lookups (ADR-0010).
        UniqueConstraint("tenant_id", "deployment_id", "kind"),
        # The job runner's poll, which spans all tenants by design. This is the one
        # deliberate exception to tenant-leading indexes: see rl_db.admin.jobs.
        Index("ix_analysis_jobs_status_run_at", "status", "run_at"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
    )
