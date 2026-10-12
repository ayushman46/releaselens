"""Trace structure: a parent/child index over spans, and the "local subtree" of an entry span.

Definitions used by self-time (:mod:`rl_core.selftime`) and by error attribution
(:mod:`rl_core.regression`):

* **Children** of a span are the spans of the *same trace* whose ``parent_span_id`` equals its
  ``span_id``, whatever their service or kind.
* The **local subtree** of an entry span ``E`` of service ``A`` is ``E`` plus every descendant
  reachable from ``E`` through spans of service ``A`` only. A span of another service ends the
  walk: it is a **boundary span** (the first span of a callee).
* **Outbound spans** are CLIENT and PRODUCER spans: the interval during which ``A`` waits for
  (or hands work to) something else, network time included.

Malformed input is tolerated rather than trusted: duplicate deliveries of the same
``(trace_id, span_id)`` keep the first copy (at-least-once ingestion can replay spans), and every
walk carries a visited set, so parent-link cycles cannot loop forever.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from rl_core.telemetry.model import Span, SpanKey, SpanKind

OUTBOUND_KINDS = frozenset({SpanKind.CLIENT, SpanKind.PRODUCER})

_NO_CHILDREN: tuple[Span, ...] = ()


@dataclass(frozen=True, slots=True, eq=False)
class TraceIndex:
    """Immutable lookup structure over a set of spans (any number of traces)."""

    by_key: Mapping[SpanKey, Span]
    children: Mapping[SpanKey, tuple[Span, ...]]
    duplicates_dropped: int

    @staticmethod
    def build(spans: Iterable[Span]) -> TraceIndex:
        by_key: dict[SpanKey, Span] = {}
        duplicates = 0
        for span in spans:
            key = span.key
            if key in by_key:
                duplicates += 1
                continue
            by_key[key] = span
        grouped: dict[SpanKey, list[Span]] = {}
        for span in by_key.values():
            parent = span.parent_key
            if parent is not None:
                grouped.setdefault(parent, []).append(span)
        children = {k: tuple(v) for k, v in grouped.items()}
        return TraceIndex(by_key=by_key, children=children, duplicates_dropped=duplicates)

    def __len__(self) -> int:
        return len(self.by_key)

    def __iter__(self) -> Iterator[Span]:
        return iter(self.by_key.values())

    def children_of(self, span: Span) -> tuple[Span, ...]:
        return self.children.get(span.key, _NO_CHILDREN)

    def parent_of(self, span: Span) -> Span | None:
        parent = span.parent_key
        return None if parent is None else self.by_key.get(parent)

    def outbound_waits(self, entry: Span) -> list[Span]:
        """Spans whose intervals are time ``entry``'s service spends waiting on other components.

        Walks ``entry``'s local subtree. Collected (and not descended into):

        * outbound spans (CLIENT/PRODUCER) of the entry's own service — the whole call, network
          included, is waiting time;
        * boundary spans (another service's span parented directly under a local span) — covers
          instrumentation that propagates context without recording a CLIENT span.

        INTERNAL/SERVER/CONSUMER spans of the same service are the service's own work: they are
        descended into, never collected, so time spent in them stays in the entry's self-time.
        """
        waits: list[Span] = []
        seen: set[SpanKey] = {entry.key}
        stack = list(self.children_of(entry))
        while stack:
            span = stack.pop()
            if span.key in seen:
                continue
            seen.add(span.key)
            if span.service != entry.service or span.kind in OUTBOUND_KINDS:
                waits.append(span)
            else:
                stack.extend(self.children_of(span))
        return waits

    def boundary_spans(self, entry: Span) -> list[Span]:
        """First spans of *other* services below ``entry`` (walking through all local spans).

        Unlike :meth:`outbound_waits` this passes through the entry service's CLIENT spans, so it
        reaches the callee's SERVER span — the span that says whether the callee itself failed.
        """
        found: list[Span] = []
        seen: set[SpanKey] = {entry.key}
        stack = list(self.children_of(entry))
        while stack:
            span = stack.pop()
            if span.key in seen:
                continue
            seen.add(span.key)
            if span.service != entry.service:
                found.append(span)
            else:
                stack.extend(self.children_of(span))
        return found

    def is_own_error(self, entry: Span) -> bool:
        """``entry`` failed and no callee it reached reported a failure.

        An error is attributed to the entry's own service when ``entry.is_error`` and none of its
        boundary spans (the callees' first spans) is an error. A callee that failed explains the
        caller's failure (propagation); a callee that answered successfully does not, even if
        something deeper below it failed and was handled. A failing outbound call that never
        reached an instrumented callee (timeout, connection refused, uninstrumented dependency)
        counts as the caller's own error: no other instrumented service can carry it.
        """
        if not entry.is_error:
            return False
        return not any(span.is_error for span in self.boundary_spans(entry))
