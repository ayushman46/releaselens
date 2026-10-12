"""deployments, analysis_jobs, outbox (M6)

Revision ID: 0002_deployments_jobs_outbox
Revises: 0001_baseline
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_deployments_jobs_outbox"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

job_kind = postgresql.ENUM(
    "early_5m", "early_10m", "final", name="analysis_job_kind", create_type=False
)
job_status = postgresql.ENUM(
    "pending",
    "running",
    "succeeded",
    "failed",
    "dead",
    name="analysis_job_status",
    create_type=False,
)


def upgrade() -> None:
    job_kind.create(op.get_bind(), checkfirst=True)
    job_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "deployments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("service", sa.String(200), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("previous_version", sa.String(200), nullable=True),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("deployer", sa.String(200), nullable=True),
        sa.Column("ci_run_url", sa.String(2048), nullable=True),
        sa.Column("repository", sa.String(201), nullable=True),
        sa.Column("pull_request", sa.Integer(), nullable=True),
        sa.Column("risk_assessment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "schema_version IN (1, 2)", name=op.f("ck_deployments_schema_version_known")
        ),
        sa.CheckConstraint(
            "pull_request IS NULL OR pull_request > 0",
            name=op.f("ck_deployments_pull_request_positive"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(labels) = 'object'", name=op.f("ck_deployments_labels_is_object")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_deployments_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint(
            "tenant_id", "event_id", name=op.f("uq_deployments_tenant_id_event_id")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_deployments_tenant_id_id")),
    )
    op.create_index(
        "ix_deployments_tenant_service_env_deployed_at",
        "deployments",
        ["tenant_id", "service", "environment", sa.text("deployed_at DESC")],
    )
    op.create_index(
        "ix_deployments_tenant_deployed_at_id",
        "deployments",
        ["tenant_id", sa.text("deployed_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "analysis_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", job_kind, nullable=False),
        sa.Column("status", job_status, server_default="pending", nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("locked_by", sa.String(200), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1", name=op.f("ck_analysis_jobs_attempts_valid")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            name=op.f("fk_analysis_jobs_tenant_id_deployments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_jobs")),
        sa.UniqueConstraint(
            "tenant_id",
            "deployment_id",
            "kind",
            name=op.f("uq_analysis_jobs_tenant_id_deployment_id_kind"),
        ),
    )
    op.create_index("ix_analysis_jobs_status_run_at", "analysis_jobs", ["status", "run_at"])

    op.create_table(
        "outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(249), nullable=False),
        sa.Column("key", sa.String(512), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_outbox_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index("ix_outbox_tenant_id_created_at", "outbox", ["tenant_id", "created_at"])
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_index("ix_outbox_tenant_id_created_at", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_analysis_jobs_status_run_at", table_name="analysis_jobs")
    op.drop_table("analysis_jobs")
    op.drop_index("ix_deployments_tenant_deployed_at_id", table_name="deployments")
    op.drop_index("ix_deployments_tenant_service_env_deployed_at", table_name="deployments")
    op.drop_table("deployments")
    job_status.drop(op.get_bind(), checkfirst=True)
    job_kind.drop(op.get_bind(), checkfirst=True)
