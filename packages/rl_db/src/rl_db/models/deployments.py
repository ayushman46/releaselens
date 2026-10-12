"""Deployments recorded from ``deployment.completed`` events (M6).

One row per *event*, made idempotent by ``UNIQUE (tenant_id, event_id)``. Kafka delivers
at least once, so the consumer inserts with ``ON CONFLICT DO NOTHING`` and a redelivered
event becomes a no-op instead of a duplicate. Rows are immutable after insert, so there is
no ``updated_at``.

Column sizes come from the event schema limits in :mod:`rl_core.events.deployment`, so
anything the parser accepts fits the column.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_core.events import deployment as limits
from rl_db.base import Base, UUIDPrimaryKeyMixin


class Deployment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "deployments"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    service: Mapped[str] = mapped_column(String(limits.MAX_SERVICE_LEN), nullable=False)
    environment: Mapped[str] = mapped_column(String(limits.MAX_ENVIRONMENT_LEN), nullable=False)
    version: Mapped[str] = mapped_column(String(limits.MAX_VERSION_LEN), nullable=False)
    previous_version: Mapped[str | None] = mapped_column(String(limits.MAX_VERSION_LEN))
    commit_sha: Mapped[str] = mapped_column(String(limits.MAX_COMMIT_SHA_LEN), nullable=False)
    deployer: Mapped[str | None] = mapped_column(String(limits.MAX_DEPLOYER_LEN))
    ci_run_url: Mapped[str | None] = mapped_column(String(limits.MAX_URL_LEN))
    repository: Mapped[str | None] = mapped_column(String(limits.MAX_REPOSITORY_LEN))
    pull_request: Mapped[int | None] = mapped_column(Integer)
    # No foreign key yet: risk_assessments arrives in M5. The id is kept as sent.
    risk_assessment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    labels: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id"),
        # Target of the composite foreign key from analysis_jobs. It keeps a job and its
        # deployment in the same tenant, and lets ON DELETE CASCADE find a deployment's
        # jobs through a tenant-leading index.
        UniqueConstraint("tenant_id", "id"),
        # "Latest deployments of service S in environment E" (regression baselines, filters).
        Index(
            "ix_deployments_tenant_service_env_deployed_at",
            "tenant_id",
            "service",
            "environment",
            text("deployed_at DESC"),
        ),
        # Unfiltered keyset pagination: ORDER BY deployed_at DESC, id DESC.
        Index(
            "ix_deployments_tenant_deployed_at_id",
            "tenant_id",
            text("deployed_at DESC"),
            text("id DESC"),
        ),
        CheckConstraint("schema_version IN (1, 2)", name="schema_version_known"),
        CheckConstraint("pull_request IS NULL OR pull_request > 0", name="pull_request_positive"),
        CheckConstraint("jsonb_typeof(labels) = 'object'", name="labels_is_object"),
    )
