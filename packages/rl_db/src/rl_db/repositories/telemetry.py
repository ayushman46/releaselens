"""Span persistence and the aggregates derived from it (tenant-scoped).

:func:`record_spans` is the span consumer's whole write path for one tenant's spans, inside the
caller's transaction:

1. ``INSERT … SELECT FROM unnest(<column arrays>) ON CONFLICT DO NOTHING RETURNING`` — one
   statement per batch, whatever its size. The rows it returns are exactly the spans this
   transaction stored; spans already present (a redelivery, a client retry) return nothing.
2. A transaction-scoped advisory lock on the tenant's edge derivation (see below).
3. The *trace view*: every stored span of the traces the batch touched.
4. Edge calls and minute rollups derived **only from the newly stored spans**
   (:mod:`rl_core.telemetry.rollup`), merged with ``INSERT … ON CONFLICT DO UPDATE`` (sums,
   ``LEAST``/``GREATEST``, element-wise histogram addition).

Redelivering a batch therefore changes nothing: no span is new, so nothing is added.

**Why the advisory lock.** A child and its parent can be stored by two transactions running at
the same time (they usually belong to different services, hence different Kafka partitions and
possibly different consumers). Under READ COMMITTED neither would see the other's uncommitted
span, and the call would be counted by neither. Holding a per-tenant lock from the trace-view
query to commit orders the two: whichever runs second sees the first one's committed span and
counts the call. Inserts still run concurrently; only derivation is serialised per tenant.

The hot path talks to asyncpg directly: binary-encoded array parameters are far faster than
SQLAlchemy's per-row parameter handling, and the statements run on the session's own connection,
inside its transaction.
"""

from __future__ import annotations

import json
import uuid
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.telemetry.model import Span, ns_to_datetime
from rl_core.telemetry.rollup import (
    EdgeIncrement,
    EdgeKey,
    MinuteRollup,
    RollupKey,
    StoredSpanRef,
    edge_increments,
    minute_rollups,
)
from rl_core.tenancy import TenantContext
from rl_db.repositories._tenancy import require_tenant, tenant_scoped

# Parent and child spans further apart than this are not linked into an edge. It bounds the
# trace-view query to a few daily partitions instead of all of them.
TRACE_LINK_WINDOW: Final = timedelta(days=1)

# pg_advisory_xact_lock(int4, int4): namespace "RL" + 2 (edge derivation), then the tenant.
_LOCK_NAMESPACE_EDGES: Final = 0x524C + 2

_INSERT_SPANS: Final = """
INSERT INTO spans (
    tenant_id, trace_id, span_id, parent_span_id, service, service_version, environment, name,
    kind, start_ns, end_ns, duration_ms, status_code, http_route, http_status, attributes,
    start_time
)
SELECT $1::uuid, t.trace_id, t.span_id, t.parent_span_id, t.service, t.service_version,
       t.environment, t.name, t.kind, t.start_ns, t.end_ns, t.duration_ms, t.status_code,
       t.http_route, t.http_status, t.attributes::jsonb, t.start_time
FROM unnest(
    $2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[],
    $9::text[], $10::int8[], $11::int8[], $12::float8[], $13::text[], $14::text[], $15::int2[],
    $16::text[], $17::timestamptz[]
) AS t(
    trace_id, span_id, parent_span_id, service, service_version, environment, name, kind,
    start_ns, end_ns, duration_ms, status_code, http_route, http_status, attributes, start_time
)
ON CONFLICT DO NOTHING
RETURNING trace_id, span_id, start_ns
"""

_TRACE_VIEW: Final = """
SELECT trace_id, span_id, parent_span_id, service, environment, start_ns
FROM spans
WHERE tenant_id = $1 AND trace_id = ANY($2::text[]) AND start_time >= $3 AND start_time <= $4
"""

_UPSERT_EDGES: Final = """
INSERT INTO service_edges AS e (
    tenant_id, environment, caller_service, callee_service, call_count, first_seen, last_seen
)
SELECT $1::uuid, x.environment, x.caller, x.callee, x.calls, x.first_seen, x.last_seen
FROM unnest($2::text[], $3::text[], $4::text[], $5::int8[], $6::timestamptz[],
            $7::timestamptz[]) AS x(environment, caller, callee, calls, first_seen, last_seen)
ON CONFLICT (tenant_id, environment, caller_service, callee_service) DO UPDATE SET
    call_count = e.call_count + EXCLUDED.call_count,
    first_seen = LEAST(e.first_seen, EXCLUDED.first_seen),
    last_seen = GREATEST(e.last_seen, EXCLUDED.last_seen)
"""

_UPSERT_ROLLUPS: Final = """
INSERT INTO service_minute_rollups AS r (
    tenant_id, environment, service, minute, request_count, error_count, duration_sum_ms,
    duration_max_ms, latency_buckets
)
SELECT $1::uuid, x.environment, x.service, x.minute, x.requests, x.errors, x.duration_sum,
       x.duration_max, x.buckets::bigint[]
FROM unnest($2::text[], $3::text[], $4::timestamptz[], $5::int8[], $6::int8[], $7::float8[],
            $8::float8[], $9::text[])
     AS x(environment, service, minute, requests, errors, duration_sum, duration_max, buckets)
ON CONFLICT (tenant_id, environment, service, minute) DO UPDATE SET
    request_count = r.request_count + EXCLUDED.request_count,
    error_count = r.error_count + EXCLUDED.error_count,
    duration_sum_ms = r.duration_sum_ms + EXCLUDED.duration_sum_ms,
    duration_max_ms = GREATEST(r.duration_max_ms, EXCLUDED.duration_max_ms),
    latency_buckets = ARRAY(
        SELECT a + b
        FROM unnest(r.latency_buckets, EXCLUDED.latency_buckets) WITH ORDINALITY AS u(a, b, i)
        ORDER BY i
    )
"""


@dataclass(frozen=True, slots=True)
class SpanWriteResult:
    received: int
    inserted: int
    edges: dict[EdgeKey, int]
    rollup_rows: int

    @property
    def duplicates(self) -> int:
        return self.received - self.inserted


async def _driver(session: AsyncSession) -> Any:
    """The asyncpg connection under ``session``, inside the session's transaction.

    SQLAlchemy's asyncpg adapter begins its transaction lazily, on the first statement; one
    trivial statement through SQLAlchemy guarantees that what runs on the driver afterwards is
    part of the caller's transaction.
    """
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    driver = raw.driver_connection
    if driver is None:  # pragma: no cover - asyncpg always exposes it
        raise RuntimeError("no driver connection")
    if not driver.is_in_transaction():
        await conn.execute(text("SELECT 1"))
    if not driver.is_in_transaction():  # pragma: no cover - defensive
        raise RuntimeError("span writes must run inside a transaction")
    return driver


def _check_tenant(ctx: TenantContext, spans: Sequence[Span]) -> None:
    require_tenant(ctx, ctx.tenant_id)
    expected = str(ctx.tenant_id)
    if any(s.tenant_id != expected for s in spans):
        raise ValueError("every span must belong to the caller's tenant")


def _dedupe(spans: Sequence[Span]) -> list[Span]:
    # Two copies of one span in one INSERT would both match the single returned row.
    seen: set[tuple[str, str, int]] = set()
    out = []
    for span in spans:
        key = (span.trace_id, span.span_id, span.start_ns)
        if key not in seen:
            seen.add(key)
            out.append(span)
    return out


@tenant_scoped
async def insert_spans(
    session: AsyncSession, ctx: TenantContext, spans: Sequence[Span]
) -> list[Span]:
    """Store ``spans``; return those this call inserted (already-stored ones are skipped)."""
    _check_tenant(ctx, spans)
    unique = _dedupe(spans)
    if not unique:
        return []
    driver = await _driver(session)
    rows = await driver.fetch(
        _INSERT_SPANS,
        ctx.tenant_id,
        [s.trace_id for s in unique],
        [s.span_id for s in unique],
        [s.parent_span_id for s in unique],
        [s.service for s in unique],
        [s.service_version for s in unique],
        [s.environment for s in unique],
        [s.name for s in unique],
        [s.kind.value for s in unique],
        [s.start_ns for s in unique],
        [s.end_ns for s in unique],
        [s.duration_ms for s in unique],
        [s.status_code.value for s in unique],
        [s.http_route for s in unique],
        [s.http_status for s in unique],
        [json.dumps(dict(s.attributes), separators=(",", ":"), ensure_ascii=False) for s in unique],
        [ns_to_datetime(s.start_ns) for s in unique],
    )
    inserted = {(r["trace_id"], r["span_id"], r["start_ns"]) for r in rows}
    return [s for s in unique if (s.trace_id, s.span_id, s.start_ns) in inserted]


def tenant_lock_key(tenant_id: uuid.UUID) -> int:
    """A stable signed 32-bit key for the tenant (crc32 of its 16 bytes)."""
    value = zlib.crc32(tenant_id.bytes)
    return value - 2**32 if value >= 2**31 else value


@tenant_scoped
async def lock_edge_derivation(session: AsyncSession, ctx: TenantContext) -> None:
    """Serialise edge derivation of this tenant until the transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _LOCK_NAMESPACE_EDGES, "key": tenant_lock_key(ctx.tenant_id)},
    )


@tenant_scoped
async def load_trace_view(
    session: AsyncSession, ctx: TenantContext, spans: Sequence[Span]
) -> list[StoredSpanRef]:
    """Every stored span of the traces of ``spans`` within :data:`TRACE_LINK_WINDOW`."""
    if not spans:
        return []
    driver = await _driver(session)
    lo = ns_to_datetime(min(s.start_ns for s in spans)) - TRACE_LINK_WINDOW
    hi = ns_to_datetime(max(s.start_ns for s in spans)) + TRACE_LINK_WINDOW
    rows = await driver.fetch(
        _TRACE_VIEW, ctx.tenant_id, sorted({s.trace_id for s in spans}), lo, hi
    )
    return [
        StoredSpanRef(
            trace_id=r["trace_id"],
            span_id=r["span_id"],
            parent_span_id=r["parent_span_id"],
            service=r["service"],
            environment=r["environment"],
            start_ns=r["start_ns"],
        )
        for r in rows
    ]


@tenant_scoped
async def add_edge_calls(
    session: AsyncSession, ctx: TenantContext, increments: Mapping[EdgeKey, EdgeIncrement]
) -> None:
    if not increments:
        return
    keys = sorted(increments)  # a fixed lock order keeps concurrent upserts deadlock-free
    driver = await _driver(session)
    await driver.execute(
        _UPSERT_EDGES,
        ctx.tenant_id,
        [k[0] for k in keys],
        [k[1] for k in keys],
        [k[2] for k in keys],
        [increments[k].calls for k in keys],
        [ns_to_datetime(increments[k].first_seen_ns) for k in keys],
        [ns_to_datetime(increments[k].last_seen_ns) for k in keys],
    )


@tenant_scoped
async def add_minute_rollups(
    session: AsyncSession, ctx: TenantContext, rollups: Mapping[RollupKey, MinuteRollup]
) -> None:
    if not rollups:
        return
    keys = sorted(rollups)
    driver = await _driver(session)
    await driver.execute(
        _UPSERT_ROLLUPS,
        ctx.tenant_id,
        [k[0] for k in keys],
        [k[1] for k in keys],
        [ns_to_datetime(k[2]) for k in keys],
        [rollups[k].request_count for k in keys],
        [rollups[k].error_count for k in keys],
        [rollups[k].duration_sum_ms for k in keys],
        [rollups[k].duration_max_ms for k in keys],
        ["{" + ",".join(map(str, rollups[k].buckets)) + "}" for k in keys],
    )


@tenant_scoped
async def fold_new_spans(
    session: AsyncSession, ctx: TenantContext, new: Sequence[Span]
) -> tuple[dict[EdgeKey, EdgeIncrement], dict[RollupKey, MinuteRollup]]:
    """Add the edge calls and minute rollups of spans *this transaction* just stored.

    Must run in the transaction that inserted ``new`` (:func:`insert_spans`), so the per-tenant
    lock it takes is held until those spans are committed.
    """
    if not new:
        return {}, {}
    _check_tenant(ctx, new)
    await lock_edge_derivation(session, ctx)
    view = await load_trace_view(session, ctx, new)
    edges = edge_increments(view, {s.key for s in new})
    rollups = minute_rollups(new)
    await add_edge_calls(session, ctx, edges)
    await add_minute_rollups(session, ctx, rollups)
    return edges, rollups


@tenant_scoped
async def record_spans(
    session: AsyncSession, ctx: TenantContext, spans: Sequence[Span]
) -> SpanWriteResult:
    """Store one tenant's spans and fold the new ones into edges and rollups (module doc).

    The caller owns the transaction; the partitions for the spans' days must already exist.
    """
    new = await insert_spans(session, ctx, spans)
    edges, rollups = await fold_new_spans(session, ctx, new)
    return SpanWriteResult(
        received=len(spans),
        inserted=len(new),
        edges={k: v.calls for k, v in edges.items()},
        rollup_rows=len(rollups),
    )
