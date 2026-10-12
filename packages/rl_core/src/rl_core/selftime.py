"""Self-time: the part of a span's duration not covered by the work it waited on.

Two definitions, both computed by the same interval-union arithmetic:

**Span self-time** (:func:`compute_self_times`) — the textbook definition::

    self(s) = duration(s) - | union { [c.start, c.end] ∩ [s.start, s.end] : c ∈ children(s) } |

Children are the spans of the same trace whose ``parent_span_id`` is ``s.span_id`` (any service,
any kind). Child intervals are clipped to the parent first (async/fire-and-forget children that
outlive the parent, or clock skew between hosts, must not push self-time below zero), then merged,
so overlapping children (concurrent calls) are subtracted once. Grandchildren never matter: they
are accounted for inside their own parent's interval.

**Service self-time** (:func:`compute_service_self_times`) — what the regression engine uses for
a service's SERVER (request) spans. For an entry SERVER span ``E`` of service ``A``::

    service_self(E) = duration(E) - | union { w ∩ E : w ∈ outbound_waits(E) } |

where ``outbound_waits(E)`` (:meth:`rl_core.telemetry.index.TraceIndex.outbound_waits`) walks
``E``'s *local subtree* — descendants reached through spans of service ``A`` — and collects the
intervals during which ``A`` waits on something else: ``A``'s CLIENT/PRODUCER spans, and any span
of another service parented directly under a local span. ``A``'s INTERNAL spans (e.g. the demo's
``payment.own_work``) are ``A``'s own work and are *not* subtracted; the walk descends through
them instead, so a CLIENT call made from inside an INTERNAL span is still subtracted.

Why the second definition: in the demo, ``payment`` does its own processing inside an INTERNAL
span and calls ``fraud`` through a CLIENT span. The span self-time of the payment SERVER span
would subtract *both* children and miss a regression inside ``payment.own_work`` entirely. The
service self-time subtracts only the CLIENT span (time waiting for fraud, network included), so a
slower fraud leaves payment's service self-time unchanged while a slower payment raises it.

All values are integer nanoseconds and every result lies in ``[0, duration]``.
"""

from __future__ import annotations

from collections.abc import Iterable

from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import Span, SpanKey, SpanKind

Interval = tuple[int, int]


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Sort and merge half-open intervals; empty or inverted intervals are dropped.

    Touching intervals (``[0, 5)`` and ``[5, 9)``) are merged too: it does not change the covered
    length and keeps the output canonical.
    """
    ordered = sorted((lo, hi) for lo, hi in intervals if hi > lo)
    merged: list[Interval] = []
    for lo, hi in ordered:
        if merged and lo <= merged[-1][1]:
            prev_lo, prev_hi = merged[-1]
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def covered_ns(intervals: Iterable[Interval], lo: int, hi: int) -> int:
    """Length of the union of ``intervals`` after clipping each one to ``[lo, hi]``."""
    clipped = ((max(a, lo), min(b, hi)) for a, b in intervals)
    return sum(b - a for a, b in merge_intervals(clipped))


def self_time_ns(span: Span, children: Iterable[Span]) -> int:
    """Span self-time of ``span`` given its direct children (see module docstring)."""
    covered = covered_ns(((c.start_ns, c.end_ns) for c in children), span.start_ns, span.end_ns)
    return span.duration_ns - covered


def compute_self_times(spans: Iterable[Span] | TraceIndex) -> dict[SpanKey, int]:
    """Span self-time for every span, keyed by ``(trace_id, span_id)``.

    Keyed by the pair rather than ``span_id`` alone because OTel only promises span ids to be
    unique within a trace.
    """
    index = spans if isinstance(spans, TraceIndex) else TraceIndex.build(spans)
    return {span.key: self_time_ns(span, index.children_of(span)) for span in index}


def service_self_time_ns(index: TraceIndex, entry: Span) -> int:
    """Service self-time of one entry span (see module docstring)."""
    waits = index.outbound_waits(entry)
    covered = covered_ns(((w.start_ns, w.end_ns) for w in waits), entry.start_ns, entry.end_ns)
    return entry.duration_ns - covered


def compute_service_self_times(
    spans: Iterable[Span] | TraceIndex,
    *,
    entries: Iterable[Span] | None = None,
) -> dict[SpanKey, int]:
    """Service self-time for entry spans, keyed by ``(trace_id, span_id)``.

    ``entries`` defaults to every SERVER span in the index; pass a subset (e.g. only the SERVER
    spans inside the analysis windows) to avoid work. Entries must belong to the index, otherwise
    their children are unknown and the result would silently equal their duration.
    """
    index = spans if isinstance(spans, TraceIndex) else TraceIndex.build(spans)
    chosen = (s for s in index if s.kind is SpanKind.SERVER) if entries is None else iter(entries)
    out: dict[SpanKey, int] = {}
    for entry in chosen:
        if entry.key not in index.by_key:
            raise KeyError(f"entry span {entry.key} is not part of the index")
        out[entry.key] = service_self_time_ns(index, entry)
    return out
