"""CI-failure risk (M5): the tenant's CI build history and the stored risk assessments.

``ci_runs`` is the serving-side counterpart of the training data's build timeline: one row per
CI build (a GitHub Actions workflow run attempt, a check suite of another CI app, or a run
reported through ``POST /api/v1/ci-runs``). Only runs that had **finished** before a request
count as history (rl_core's history rule), so ``finished_at`` is what the history query sorts on.

``risk_assessments`` keeps everything needed to explain an assessment later without the model:
the feature vector, the change facts as sent, the model version, the raw and user-facing scores
and the SHAP vector. Rows are immutable after insert.

Repository names are stored lower-cased (``owner/repo``; GitHub names are case-insensitive), so
the Action, the GitHub App and the API all address one repository the same way.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
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
from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

CI_RUN_STATUSES = ("passed", "failed", "errored", "canceled", "running")
FINISHED_WITH_OUTCOME = ("passed", "failed", "errored")  # eligible as history (ADR-0005)
FAILED_STATUSES = ("failed", "errored")  # label 1 in the primary variant (ADR-0005)
CI_RUN_SOURCES = ("github_app", "action", "api")
RISK_EVENTS = ("push", "pull_request")
RISK_BANDS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

MAX_BRANCH_LEN = 255
MAX_EXTERNAL_ID_LEN = 200
MAX_MODEL_NAME_LEN = 100
MAX_MODEL_VERSION_LEN = 120


class CiRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ci_runs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    repository: Mapped[str] = mapped_column(String(limits.MAX_REPOSITORY_LEN), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(limits.MAX_COMMIT_SHA_LEN), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(MAX_BRANCH_LEN))
    is_pr: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    pull_request: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_s: Mapped[float | None] = mapped_column(Double)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(MAX_EXTERNAL_ID_LEN), nullable=False)

    __table_args__ = (
        # Idempotent ingestion: a redelivered webhook or a retried POST updates, never duplicates.
        UniqueConstraint("tenant_id", "source", "external_id"),
        # The history query: finished runs of one repository, newest finish first.
        Index(
            "ix_ci_runs_tenant_repo_finished_at",
            "tenant_id",
            "repository",
            text("finished_at DESC"),
        ),
        # The latest start before a request (hours_since_last_build) and listing by start.
        Index(
            "ix_ci_runs_tenant_repo_started_at", "tenant_id", "repository", text("started_at DESC")
        ),
        # Cross-source deduplication (the same workflow run reported by the App and the API).
        Index("ix_ci_runs_tenant_repo_external_id", "tenant_id", "repository", "external_id"),
        # "CI outcome of this commit" (GET /api/v1/ci-runs?commit_sha=), newest start first.
        Index("ix_ci_runs_tenant_commit_sha", "tenant_id", "commit_sha", text("started_at DESC")),
        CheckConstraint(
            "status IN ('passed', 'failed', 'errored', 'canceled', 'running')", name="status_known"
        ),
        CheckConstraint("source IN ('github_app', 'action', 'api')", name="source_known"),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL) OR status = 'canceled'",
            name="finished_matches_status",
        ),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="finish_order"),
        CheckConstraint("duration_s IS NULL OR duration_s >= 0", name="duration_nonnegative"),
        CheckConstraint("pull_request IS NULL OR pull_request > 0", name="pull_request_positive"),
    )


class RiskAssessment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "risk_assessments"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    repository: Mapped[str] = mapped_column(String(limits.MAX_REPOSITORY_LEN), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(limits.MAX_COMMIT_SHA_LEN), nullable=False)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    pull_request: Mapped[int | None] = mapped_column(Integer)
    branch: Mapped[str | None] = mapped_column(String(MAX_BRANCH_LEN))
    ci_run_url: Mapped[str | None] = mapped_column(String(limits.MAX_URL_LEN))
    previous_built_sha: Mapped[str | None] = mapped_column(String(limits.MAX_COMMIT_SHA_LEN))
    extractor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_name: Mapped[str] = mapped_column(String(MAX_MODEL_NAME_LEN), nullable=False)
    model_version: Mapped[str] = mapped_column(String(MAX_MODEL_VERSION_LEN), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_score: Mapped[float] = mapped_column(Double, nullable=False)
    risk_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    risk_band: Mapped[str] = mapped_column(String(16), nullable=False)
    probability: Mapped[float | None] = mapped_column(Double)
    # {"unit": "log-odds", "base_value", "values": {feature: contribution}, "top_increasing",
    #  "top_reducing", "note"}
    shap: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    history_used: Mapped[int] = mapped_column(Integer, nullable=False)
    cold_start: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Keyset pagination: ORDER BY created_at DESC, id DESC.
        Index(
            "ix_risk_assessments_tenant_created_at_id",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # Idempotency lookup (and "assessments of this commit").
        Index(
            "ix_risk_assessments_tenant_repo_sha",
            "tenant_id",
            "repository",
            "commit_sha",
            text("created_at DESC"),
        ),
        CheckConstraint("event IN ('push', 'pull_request')", name="event_known"),
        CheckConstraint(
            "risk_band IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')", name="risk_band_known"
        ),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="risk_score_range"),
        CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="probability_range",
        ),
        CheckConstraint("history_used >= 0", name="history_used_nonnegative"),
        CheckConstraint("pull_request IS NULL OR pull_request > 0", name="pull_request_positive"),
        CheckConstraint("jsonb_typeof(features) = 'object'", name="features_is_object"),
        CheckConstraint("jsonb_typeof(change_facts) = 'object'", name="change_facts_is_object"),
    )
