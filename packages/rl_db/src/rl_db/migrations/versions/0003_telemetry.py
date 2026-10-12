"""spans (partitioned by day), service_edges, service_minute_rollups, regression_analyses (M7/M8)

Revision ID: 0003_telemetry
Revises: 0002_deployments_jobs_outbox
Create Date: 2026-10-13

``spans`` is created with raw DDL: Alembic cannot express ``PARTITION BY``. The ORM model
(``rl_db.models.telemetry.SpanRecord``) mirrors it column for column, and the migration test
compares both. No partition is created here: ``rl_db.admin.partitions.ensure_partitions`` creates
them ahead of time (worker maintenance loop and span consumer), and dropping a day's partition is
the retention mechanism. Downgrading drops the partitions with their parent.

Values below are written out rather than imported, so this revision stays frozen in time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_telemetry"
down_revision: str | None = "0002_deployments_jobs_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LATENCY_BUCKETS = 42  # rl_core.telemetry.rollup.LATENCY_BUCKETS at the time of this revision

# Existing type from 0002; never created or dropped here.
job_kind = postgresql.ENUM(
    "early_5m", "early_10m", "final", name="analysis_job_kind", create_type=False
)

SPANS_DDL = """
CREATE TABLE spans (
    tenant_id       uuid             NOT NULL,
    trace_id        varchar(32)      NOT NULL,
    span_id         varchar(16)      NOT NULL,
    parent_span_id  varchar(16),
    service         varchar(200)     NOT NULL,
    service_version varchar(200),
    environment     varchar(64),
    name            varchar(512)     NOT NULL,
    kind            varchar(16)      NOT NULL,
    start_ns        bigint           NOT NULL,
    end_ns          bigint           NOT NULL,
    duration_ms     double precision NOT NULL,
    status_code     varchar(8)       NOT NULL,
    http_route      varchar(512),
    http_status     smallint,
    attributes      jsonb            NOT NULL DEFAULT '{}'::jsonb,
    start_time      timestamptz      NOT NULL,
    CONSTRAINT pk_spans PRIMARY KEY (tenant_id, trace_id, span_id, start_time),
    CONSTRAINT ck_spans_kind_known
        CHECK (kind IN ('SERVER', 'CLIENT', 'INTERNAL', 'PRODUCER', 'CONSUMER')),
    CONSTRAINT ck_spans_status_code_known CHECK (status_code IN ('UNSET', 'OK', 'ERROR')),
    CONSTRAINT ck_spans_end_after_start CHECK (end_ns >= start_ns)
) PARTITION BY RANGE (start_time)
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.execute(SPANS_DDL)
    # The primary key's leading columns serve (tenant_id, trace_id) lookups; see the model.
    op.create_index(
        "ix_spans_tenant_env_service_start_time",
        "spans",
        ["tenant_id", "environment", "service", "start_time"],
    )

    op.create_table(
        "service_edges",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("caller_service", sa.String(200), nullable=False),
        sa.Column("callee_service", sa.String(200), nullable=False),
        sa.Column("call_count", sa.BigInteger(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("call_count > 0", name=op.f("ck_service_edges_call_count_positive")),
        sa.CheckConstraint(
            "caller_service <> callee_service", name=op.f("ck_service_edges_no_self_loop")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_service_edges_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "environment",
            "caller_service",
            "callee_service",
            name=op.f("pk_service_edges"),
        ),
    )

    op.create_table(
        "service_minute_rollups",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("service", sa.String(200), nullable=False),
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("error_count", sa.BigInteger(), nullable=False),
        sa.Column("duration_sum_ms", sa.Double(), nullable=False),
        sa.Column("duration_max_ms", sa.Double(), nullable=False),
        sa.Column("latency_buckets", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.CheckConstraint(
            "request_count >= 0 AND error_count >= 0 AND error_count <= request_count",
            name=op.f("ck_service_minute_rollups_counts_valid"),
        ),
        sa.CheckConstraint(
            f"cardinality(latency_buckets) = {LATENCY_BUCKETS}",
            name=op.f("ck_service_minute_rollups_latency_buckets_size"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_service_minute_rollups_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "environment", "service", "minute", name=op.f("pk_service_minute_rollups")
        ),
    )

    op.create_table(
        "regression_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", job_kind, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("severity_score", sa.Double(), nullable=True),
        sa.Column("severity_band", sa.String(16), nullable=True),
        sa.Column("probable_origin", sa.String(200), nullable=True),
        sa.Column("confidence", sa.String(8), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("windows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "sampling",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            _in("status", ("HEALTHY", "WARNING", "REGRESSED", "INSUFFICIENT_DATA")),
            name=op.f("ck_regression_analyses_status_known"),
        ),
        sa.CheckConstraint(
            "severity_band IS NULL OR "
            + _in("severity_band", ("HEALTHY", "WARNING", "HIGH", "CRITICAL")),
            name=op.f("ck_regression_analyses_severity_band_known"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR " + _in("confidence", ("HIGH", "MEDIUM", "LOW")),
            name=op.f("ck_regression_analyses_confidence_known"),
        ),
        sa.CheckConstraint(
            "severity_score IS NULL OR (severity_score >= 0 AND severity_score <= 100)",
            name=op.f("ck_regression_analyses_severity_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
            name=op.f("fk_regression_analyses_tenant_id_deployments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regression_analyses")),
        sa.UniqueConstraint(
            "tenant_id",
            "deployment_id",
            "kind",
            name=op.f("uq_regression_analyses_tenant_id_deployment_id_kind"),
        ),
    )
    op.create_index(
        "ix_regression_analyses_tenant_created_at",
        "regression_analyses",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_regression_analyses_tenant_created_at", table_name="regression_analyses")
    op.drop_table("regression_analyses")
    op.drop_table("service_minute_rollups")
    op.drop_table("service_edges")
    # Drops every partition with it.
    op.execute("DROP TABLE spans")
