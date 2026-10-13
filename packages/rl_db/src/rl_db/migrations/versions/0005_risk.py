"""CI-failure risk (M5): ci_runs, risk_assessments, GitHub App installations, webhook deliveries,
pull requests

Revision ID: 0005_risk
Revises: 0004_saas
Create Date: 2026-10-13

Self-contained: it only adds tables, so a downgrade drops exactly them.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_risk"
down_revision: str | None = "0004_saas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _now() -> sa.TextClause:
    return sa.text("now()")


def _created_updated() -> list[sa.Column[datetime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_tenant_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ ci_runs
    op.create_table(
        "ci_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("repository", sa.String(201), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("is_pr", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("pull_request", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_s", sa.Double(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        *_created_updated(),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'errored', 'canceled', 'running')",
            name=op.f("ck_ci_runs_status_known"),
        ),
        sa.CheckConstraint(
            "source IN ('github_app', 'action', 'api')", name=op.f("ck_ci_runs_source_known")
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL) OR status = 'canceled'",
            name=op.f("ck_ci_runs_finished_matches_status"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_ci_runs_finish_order"),
        ),
        sa.CheckConstraint(
            "duration_s IS NULL OR duration_s >= 0", name=op.f("ck_ci_runs_duration_nonnegative")
        ),
        sa.CheckConstraint(
            "pull_request IS NULL OR pull_request > 0",
            name=op.f("ck_ci_runs_pull_request_positive"),
        ),
        _tenant_fk("ci_runs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ci_runs")),
        sa.UniqueConstraint(
            "tenant_id",
            "source",
            "external_id",
            name=op.f("uq_ci_runs_tenant_id_source_external_id"),
        ),
    )
    op.create_index(
        "ix_ci_runs_tenant_repo_finished_at",
        "ci_runs",
        ["tenant_id", "repository", sa.text("finished_at DESC")],
    )
    op.create_index(
        "ix_ci_runs_tenant_repo_started_at",
        "ci_runs",
        ["tenant_id", "repository", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_ci_runs_tenant_repo_external_id",
        "ci_runs",
        ["tenant_id", "repository", "external_id"],
    )

    # ------------------------------------------------------------------ risk_assessments
    op.create_table(
        "risk_assessments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("repository", sa.String(201), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("pull_request", sa.Integer(), nullable=True),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("ci_run_url", sa.String(2048), nullable=True),
        sa.Column("previous_built_sha", sa.String(64), nullable=True),
        sa.Column("extractor", JSONB, nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", JSONB, nullable=False),
        sa.Column("change_facts", JSONB, nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("feature_schema_version", sa.String(16), nullable=False),
        sa.Column("raw_score", sa.Double(), nullable=False),
        sa.Column("risk_score", sa.SmallInteger(), nullable=False),
        sa.Column("risk_band", sa.String(16), nullable=False),
        sa.Column("probability", sa.Double(), nullable=True),
        sa.Column("shap", JSONB, nullable=False),
        sa.Column("history_used", sa.Integer(), nullable=False),
        sa.Column("cold_start", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "event IN ('push', 'pull_request')", name=op.f("ck_risk_assessments_event_known")
        ),
        sa.CheckConstraint(
            "risk_band IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
            name=op.f("ck_risk_assessments_risk_band_known"),
        ),
        sa.CheckConstraint(
            "risk_score BETWEEN 0 AND 100", name=op.f("ck_risk_assessments_risk_score_range")
        ),
        sa.CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name=op.f("ck_risk_assessments_probability_range"),
        ),
        sa.CheckConstraint(
            "history_used >= 0", name=op.f("ck_risk_assessments_history_used_nonnegative")
        ),
        sa.CheckConstraint(
            "pull_request IS NULL OR pull_request > 0",
            name=op.f("ck_risk_assessments_pull_request_positive"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(features) = 'object'", name=op.f("ck_risk_assessments_features_is_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(change_facts) = 'object'",
            name=op.f("ck_risk_assessments_change_facts_is_object"),
        ),
        _tenant_fk("risk_assessments"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_assessments")),
    )
    op.create_index(
        "ix_risk_assessments_tenant_created_at_id",
        "risk_assessments",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_risk_assessments_tenant_repo_sha",
        "risk_assessments",
        ["tenant_id", "repository", "commit_sha", sa.text("created_at DESC")],
    )

    # ------------------------------------------------------------------ GitHub App
    op.create_table(
        "github_installations",
        sa.Column("installation_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("account_login", sa.String(39), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        *_created_updated(),
        sa.CheckConstraint(
            "installation_id > 0", name=op.f("ck_github_installations_installation_id_positive")
        ),
        sa.CheckConstraint(
            "account_login = lower(account_login)",
            name=op.f("ck_github_installations_account_login_lower"),
        ),
        _tenant_fk("github_installations"),
        sa.PrimaryKeyConstraint("installation_id", name=op.f("pk_github_installations")),
    )
    op.create_index(
        "ix_github_installations_tenant_account",
        "github_installations",
        ["tenant_id", "account_login"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.String(100), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        _tenant_fk("webhook_deliveries"),
        sa.PrimaryKeyConstraint("delivery_id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index("ix_webhook_deliveries_received_at", "webhook_deliveries", ["received_at"])
    op.create_index(
        "ix_webhook_deliveries_tenant_received_at",
        "webhook_deliveries",
        ["tenant_id", "received_at"],
    )

    op.create_table(
        "github_pull_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("repository", sa.String(201), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("base_ref", sa.String(255), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("merged", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("draft", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("github_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint("number > 0", name=op.f("ck_github_pull_requests_number_positive")),
        sa.CheckConstraint(
            "state IN ('open', 'closed')", name=op.f("ck_github_pull_requests_state_known")
        ),
        _tenant_fk("github_pull_requests"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_pull_requests")),
        sa.UniqueConstraint(
            "tenant_id",
            "repository",
            "number",
            name=op.f("uq_github_pull_requests_tenant_id_repository_number"),
        ),
    )


def downgrade() -> None:
    op.drop_table("github_pull_requests")
    op.drop_index("ix_webhook_deliveries_tenant_received_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_received_at", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_github_installations_tenant_account", table_name="github_installations")
    op.drop_table("github_installations")
    op.drop_index("ix_risk_assessments_tenant_repo_sha", table_name="risk_assessments")
    op.drop_index("ix_risk_assessments_tenant_created_at_id", table_name="risk_assessments")
    op.drop_table("risk_assessments")
    op.drop_index("ix_ci_runs_tenant_repo_external_id", table_name="ci_runs")
    op.drop_index("ix_ci_runs_tenant_repo_started_at", table_name="ci_runs")
    op.drop_index("ix_ci_runs_tenant_repo_finished_at", table_name="ci_runs")
    op.drop_table("ci_runs")
