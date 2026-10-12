"""Telemetry storage (M7, ADR-0008): raw spans, service edges and per-minute service rollups.

``spans`` is **declaratively partitioned by day** on ``start_time`` (``start_ns`` as a
``timestamptz``, truncated to microseconds). The parent table here is what reads and writes
target; Postgres routes rows to the partition of their day. Partitions are created ahead of time
and dropped for retention by :mod:`rl_db.admin.partitions`; there is deliberately no DEFAULT
partition (a default partition would have to be scanned whenever a new day's partition is
attached).

The partition key must be part of every unique constraint of a partitioned table, so the primary
key is ``(tenant_id, trace_id, span_id, start_time)``. Its leading columns also serve the
``(tenant_id, trace_id)`` trace lookups, so no separate index exists for them: one fewer index to
maintain on the hottest insert path.

Column sizes come from the wire limits in :mod:`rl_core.events.spans`, so anything the ingest
accepts fits.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_core.events import deployment as deployment_limits
from rl_core.events import spans as span_limits
from rl_core.telemetry.rollup import LATENCY_BUCKETS
from rl_db.base import Base

SPAN_KINDS = ("SERVER", "CLIENT", "INTERNAL", "PRODUCER", "CONSUMER")
STATUS_CODES = ("UNSET", "OK", "ERROR")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SpanRecord(Base):
    __tablename__ = "spans"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    service: Mapped[str] = mapped_column(String(deployment_limits.MAX_SERVICE_LEN), nullable=False)
    service_version: Mapped[str | None] = mapped_column(String(span_limits.MAX_VERSION_LEN))
    # NULL when the resource did not report deployment.environment.
    environment: Mapped[str | None] = mapped_column(String(deployment_limits.MAX_ENVIRONMENT_LEN))
    name: Mapped[str] = mapped_column(String(span_limits.MAX_SPAN_NAME_LEN), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    start_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Double, nullable=False)
    status_code: Mapped[str] = mapped_column(String(8), nullable=False)
    http_route: Mapped[str | None] = mapped_column(String(span_limits.MAX_HTTP_ROUTE_LEN))
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Partition key: start_ns as timestamptz (microsecond precision). Declared last so the
    # primary key reads (tenant_id, trace_id, span_id, start_time).
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    __table_args__ = (
        # The regression engine's window query and the service catalog.
        Index(
            "ix_spans_tenant_env_service_start_time",
            "tenant_id",
            "environment",
            "service",
            "start_time",
        ),
        CheckConstraint(_in("kind", SPAN_KINDS), name="kind_known"),
        CheckConstraint(_in("status_code", STATUS_CODES), name="status_code_known"),
        CheckConstraint("end_ns >= start_ns", name="end_after_start"),
        {"postgresql_partition_by": "RANGE (start_time)"},
    )


class ServiceEdge(Base):
    """``caller_service → callee_service`` call counts (see rl_core.telemetry.rollup).

    ``environment`` is ``''`` when neither span reported one (a primary-key column cannot be
    NULL, and ``''`` is never a valid environment name).
    """

    __tablename__ = "service_edges"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    environment: Mapped[str] = mapped_column(
        String(deployment_limits.MAX_ENVIRONMENT_LEN), primary_key=True
    )
    caller_service: Mapped[str] = mapped_column(
        String(deployment_limits.MAX_SERVICE_LEN), primary_key=True
    )
    callee_service: Mapped[str] = mapped_column(
        String(deployment_limits.MAX_SERVICE_LEN), primary_key=True
    )
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("call_count > 0", name="call_count_positive"),
        CheckConstraint("caller_service <> callee_service", name="no_self_loop"),
    )


class ServiceMinuteRollup(Base):
    """Requests (SERVER spans) of one service in one minute, mergeable by addition.

    ``latency_buckets`` is the fixed log-scale histogram of
    :data:`rl_core.telemetry.rollup.LATENCY_BUCKET_BOUNDS_MS` (42 counts, overflow last).
    """

    __tablename__ = "service_minute_rollups"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    environment: Mapped[str] = mapped_column(
        String(deployment_limits.MAX_ENVIRONMENT_LEN), primary_key=True
    )
    service: Mapped[str] = mapped_column(
        String(deployment_limits.MAX_SERVICE_LEN), primary_key=True
    )
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_sum_ms: Mapped[float] = mapped_column(Double, nullable=False)
    duration_max_ms: Mapped[float] = mapped_column(Double, nullable=False)
    latency_buckets: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "request_count >= 0 AND error_count >= 0 AND error_count <= request_count",
            name="counts_valid",
        ),
        CheckConstraint(
            f"cardinality(latency_buckets) = {LATENCY_BUCKETS}", name="latency_buckets_size"
        ),
    )
