"""Incremental aggregates over ingested spans: service edges and per-minute service rollups.

Both are derived by the span consumer from **newly inserted** spans only, in the transaction that
inserts them, so a redelivered batch (already stored) adds nothing. This module holds the pure part:
what a set of new spans contributes. Storage and merging live in ``rl_db``.

**Edges.** ``A → B`` gains one call for every span of service ``B`` whose parent span (same
trace) belongs to service ``A ≠ B`` — the definition :mod:`rl_core.graph` uses. A parent and
its child can arrive in different batches, in either order, so every newly stored span is looked
at from both sides against a *trace view* (every stored span of the batch's traces, the new ones
included):

* a new span whose parent is in the view contributes its own edge;
* a new span contributes the edges of children that were stored **earlier** (children stored
  in the same transaction contribute through the first rule).

Each parent/child pair is therefore counted exactly once, when the second of the two is stored.

**Minute rollups** come from SERVER spans only (a SERVER span is one request, as in
:mod:`rl_core.regression.metrics`), bucketed by the minute their start falls in. Latency goes into
a fixed log-scale histogram (:data:`LATENCY_BUCKET_BOUNDS_MS`) so rollups merge by addition and
percentiles can be *approximated* from them (:func:`approx_quantile`). Exact percentiles for the
regression engine are always computed from raw spans, never from rollups.

Environment ``""`` stands for "not reported" in edge and rollup keys (``EnvironmentName`` requires
at least one character, so it cannot clash with a real environment).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from rl_core.telemetry.model import NS_PER_S, Span, SpanKey, SpanKind

UNKNOWN_ENVIRONMENT: Final = ""

#: Upper bounds (inclusive, milliseconds) of the latency histogram buckets: ``2 ** (k / 2)`` for
#: ``k = -6 … 34``, i.e. 0.125 ms … 131 072 ms (≈ 2.2 min), a factor of √2 between neighbours.
#: Bucket ``i`` counts durations ``d`` with ``bounds[i-1] < d <= bounds[i]`` (bucket 0 also takes
#: ``d = 0``); one extra overflow bucket counts ``d > 131 072 ms``. **The bounds are part of the
#: stored schema**: changing them makes existing rows unmergeable and needs a migration.
LATENCY_BUCKET_BOUNDS_MS: Final[tuple[float, ...]] = tuple(2.0 ** (k / 2) for k in range(-6, 35))
LATENCY_BUCKETS: Final = len(LATENCY_BUCKET_BOUNDS_MS) + 1  # 42, including the overflow bucket

_NS_PER_MINUTE: Final = 60 * NS_PER_S


def bucket_index(duration_ms: float) -> int:
    """Histogram bucket of a duration (see :data:`LATENCY_BUCKET_BOUNDS_MS`)."""
    if duration_ms < 0 or math.isnan(duration_ms):
        raise ValueError("duration must be a non-negative number")
    return bisect_left(LATENCY_BUCKET_BOUNDS_MS, duration_ms)


def bucket_range_ms(index: int) -> tuple[float, float]:
    """``(lower, upper)`` bounds of bucket ``index``; the overflow bucket's upper is ``inf``."""
    if not 0 <= index < LATENCY_BUCKETS:
        raise IndexError(index)
    lower = 0.0 if index == 0 else LATENCY_BUCKET_BOUNDS_MS[index - 1]
    upper = math.inf if index == LATENCY_BUCKETS - 1 else LATENCY_BUCKET_BOUNDS_MS[index]
    return lower, upper


def merge_histograms(a: Iterable[int], b: Iterable[int]) -> list[int]:
    left, right = list(a), list(b)
    if len(left) != LATENCY_BUCKETS or len(right) != LATENCY_BUCKETS:
        raise ValueError(f"histograms must have {LATENCY_BUCKETS} buckets")
    return [x + y for x, y in zip(left, right, strict=True)]


@dataclass(frozen=True, slots=True)
class QuantileEstimate:
    """An approximate percentile from a histogram: ``value_ms`` lies in ``[low_ms, high_ms]``.

    Within the bucket the value is interpolated geometrically (log-linear; linearly in the first
    bucket, whose lower bound is 0). The true percentile is guaranteed to lie in the bucket's
    bounds, which are at most a factor √2 apart; in the overflow bucket only the lower bound is
    known, and ``value_ms`` is that lower bound.
    """

    value_ms: float
    low_ms: float
    high_ms: float


def approx_quantile(counts: Iterable[int], q: float) -> QuantileEstimate | None:
    """Approximate ``q``-quantile (``0 < q < 1``) of a histogram; ``None`` when it is empty."""
    if not 0 < q < 1:
        raise ValueError("q must be within (0, 1)")
    hist = list(counts)
    if len(hist) != LATENCY_BUCKETS or any(c < 0 for c in hist):
        raise ValueError(f"a histogram has {LATENCY_BUCKETS} non-negative counts")
    total = sum(hist)
    if total == 0:
        return None
    rank = q * total
    cumulative = 0
    for index, count in enumerate(hist):
        if count == 0:
            continue
        if cumulative + count >= rank:
            low, high = bucket_range_ms(index)
            if math.isinf(high):
                return QuantileEstimate(value_ms=low, low_ms=low, high_ms=high)
            fraction = min(1.0, max(0.0, (rank - cumulative) / count))
            value = low + fraction * (high - low) if low == 0 else low * (high / low) ** fraction
            return QuantileEstimate(value_ms=value, low_ms=low, high_ms=high)
        cumulative += count
    raise AssertionError("unreachable: rank <= total")  # pragma: no cover


# ----------------------------------------------------------------------------- rollups
def minute_floor_ns(start_ns: int) -> int:
    return start_ns - start_ns % _NS_PER_MINUTE


@dataclass(slots=True)
class MinuteRollup:
    """Aggregate of one service's requests (SERVER spans) that started in one minute."""

    request_count: int = 0
    error_count: int = 0
    duration_sum_ms: float = 0.0
    duration_max_ms: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * LATENCY_BUCKETS)

    def add(self, span: Span) -> None:
        duration = span.duration_ms
        self.request_count += 1
        self.error_count += int(span.is_error)
        self.duration_sum_ms += duration
        self.duration_max_ms = max(self.duration_max_ms, duration)
        self.buckets[bucket_index(duration)] += 1


RollupKey = tuple[str, str, int]
"""``(environment, service, minute_start_ns)``."""


def minute_rollups(spans: Iterable[Span]) -> dict[RollupKey, MinuteRollup]:
    """Per-minute rollups of the SERVER spans in ``spans`` (every other kind is ignored)."""
    out: dict[RollupKey, MinuteRollup] = {}
    for span in spans:
        if span.kind is not SpanKind.SERVER:
            continue
        key = (
            span.environment or UNKNOWN_ENVIRONMENT,
            span.service,
            minute_floor_ns(span.start_ns),
        )
        rollup = out.get(key)
        if rollup is None:
            rollup = out[key] = MinuteRollup()
        rollup.add(span)
    return out


# ----------------------------------------------------------------------------- edges
@dataclass(frozen=True, slots=True)
class StoredSpanRef:
    """What edge derivation needs to know about a stored span."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    environment: str | None
    start_ns: int

    @property
    def key(self) -> SpanKey:
        return (self.trace_id, self.span_id)


EdgeKey = tuple[str, str, str]
"""``(environment, caller_service, callee_service)``."""


@dataclass(slots=True)
class EdgeIncrement:
    calls: int = 0
    first_seen_ns: int = 0
    last_seen_ns: int = 0

    def add(self, at_ns: int) -> None:
        if self.calls == 0:
            self.first_seen_ns = self.last_seen_ns = at_ns
        else:
            self.first_seen_ns = min(self.first_seen_ns, at_ns)
            self.last_seen_ns = max(self.last_seen_ns, at_ns)
        self.calls += 1


def edge_increments(
    trace_view: Iterable[StoredSpanRef], new_keys: Collection[SpanKey]
) -> dict[EdgeKey, EdgeIncrement]:
    """Edge calls contributed by the spans in ``new_keys`` (see module docstring).

    ``trace_view`` must hold every stored span of the traces touched by the new spans, the new
    spans included. A call's environment is the callee's, else the caller's, else ``""``; its
    time (``first_seen``/``last_seen``) is the callee span's start.
    """
    by_key: dict[SpanKey, StoredSpanRef] = {}
    children: dict[SpanKey, list[StoredSpanRef]] = {}
    for ref in trace_view:
        if ref.key in by_key:
            continue
        by_key[ref.key] = ref
        if ref.parent_span_id is not None:
            children.setdefault((ref.trace_id, ref.parent_span_id), []).append(ref)

    out: dict[EdgeKey, EdgeIncrement] = {}

    def count(parent: StoredSpanRef, child: StoredSpanRef) -> None:
        if parent.service == child.service:
            return
        env = child.environment or parent.environment or UNKNOWN_ENVIRONMENT
        key = (env, parent.service, child.service)
        increment = out.get(key)
        if increment is None:
            increment = out[key] = EdgeIncrement()
        increment.add(child.start_ns)

    new = set(new_keys)
    for key in new:
        span = by_key.get(key)
        if span is None:
            continue
        # The new span as a child: its parent may be new too, or stored earlier.
        if span.parent_span_id is not None:
            parent = by_key.get((span.trace_id, span.parent_span_id))
            if parent is not None:
                count(parent, span)
        # The new span as a parent of children stored earlier (new children counted above).
        for child in children.get(key, ()):
            if child.key not in new:
                count(span, child)
    return out


def edge_increments_as_mapping(
    increments: Mapping[EdgeKey, EdgeIncrement],
) -> dict[EdgeKey, int]:
    """``{edge: calls}`` (handy for tests and logs)."""
    return {k: v.calls for k, v in increments.items()}
