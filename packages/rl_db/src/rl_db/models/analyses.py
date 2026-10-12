"""Regression analyses (M8): one row per deployment and analysis kind.

The analysis job handler upserts the row for its ``(deployment, kind)`` and adds an
``analysis.results`` outbox row in the same transaction. ``result`` is the complete
:class:`rl_core.regression.analysis.RegressionAnalysis` JSON (evidence, windows, config, graph,
triage, blast radius) of ``engine_version``; the scalar columns repeat what lists and filters
need. Wording rule: ``probable_origin``, never "root cause".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    Enum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_core.events import deployment as deployment_limits
from rl_db.base import Base, UUIDPrimaryKeyMixin
from rl_db.models.jobs import JobKind

ANALYSIS_STATUSES = ("HEALTHY", "WARNING", "REGRESSED", "INSUFFICIENT_DATA")
SEVERITY_BANDS = ("HEALTHY", "WARNING", "HIGH", "CRITICAL")
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class RegressionAnalysisRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "regression_analyses"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deployment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Same Postgres enum as analysis_jobs.kind; its declaration order (early_5m < early_10m <
    # final) makes ORDER BY kind DESC put the most complete analysis first.
    kind: Mapped[JobKind] = mapped_column(
        Enum(
            JobKind,
            name="analysis_job_kind",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    severity_score: Mapped[float | None] = mapped_column(Double)
    severity_band: Mapped[str | None] = mapped_column(String(16))
    probable_origin: Mapped[str | None] = mapped_column(String(deployment_limits.MAX_SERVICE_LEN))
    confidence: Mapped[str | None] = mapped_column(String(8))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    windows: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # How the input spans were loaded: counts, and the trace-hash sample when the window held
    # more spans than the handler's limit.
    sampling: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # An analysis can only point at a deployment of its own tenant.
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            ondelete="CASCADE",
        ),
        # One analysis per kind per deployment (the upsert target). Deployment ids are globally
        # unique, so this is UNIQUE(deployment_id, kind) with tenant_id leading (ADR-0010).
        UniqueConstraint("tenant_id", "deployment_id", "kind"),
        Index("ix_regression_analyses_tenant_created_at", "tenant_id", "created_at"),
        CheckConstraint(_in("status", ANALYSIS_STATUSES), name="status_known"),
        CheckConstraint(
            f"severity_band IS NULL OR {_in('severity_band', SEVERITY_BANDS)}",
            name="severity_band_known",
        ),
        CheckConstraint(
            f"confidence IS NULL OR {_in('confidence', CONFIDENCES)}", name="confidence_known"
        ),
        CheckConstraint(
            "severity_score IS NULL OR (severity_score >= 0 AND severity_score <= 100)",
            name="severity_score_range",
        ),
    )
