"""Self-time: exact topology checks + Hypothesis properties against a brute-force oracle."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tracegen import make_span

from rl_core.selftime import (
    compute_self_times,
    compute_service_self_times,
    covered_ns,
    merge_intervals,
    self_time_ns,
)
from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind

C, S, IN = SpanKind.CLIENT, SpanKind.SERVER, SpanKind.INTERNAL


def _ms(value_ns: int) -> float:
    return value_ns / NS_PER_MS


# ----------------------------------------------------------------------------- intervals


def test_merge_intervals() -> None:
    assert merge_intervals([]) == []
    assert merge_intervals([(5, 9), (0, 3), (2, 4), (9, 10), (7, 7), (8, 6)]) == [(0, 4), (5, 10)]


def test_covered_clips_to_bounds() -> None:
    assert covered_ns([(-5, 5), (8, 20)], 0, 10) == 5 + 2
    assert covered_ns([(20, 30)], 0, 10) == 0


# ----------------------------------------------------------------------------- spec topology


def _payment_fraud(
    payment_own_ms: float = 60.0, fraud_ms: float = 336.0
) -> tuple[list[Span], dict[str, int]]:
    """payment SERVER → {payment.own_work INTERNAL, payment CLIENT → fraud SERVER → own_work}.

    Times in ms: own work [1, 1+own], CLIENT starts 1 ms after own work and lasts fraud + 2 ms
    (1 ms network each way), payment answers 100 ms after the CLIENT returns.
    """
    own_end = 1 + payment_own_ms
    c_start = own_end + 1
    f_start = c_start + 1
    f_end = f_start + fraud_ms
    c_end = f_end + 1
    p_end = c_end + 100
    spans = [
        make_span("ps", None, "payment", S, 0, p_end),
        make_span("pw", "ps", "payment", IN, 1, own_end),
        make_span("pc", "ps", "payment", C, c_start, c_end),
        make_span("fs", "pc", "fraud", S, f_start, f_end),
        make_span("fw", "fs", "fraud", IN, f_start + 1, f_end - 1),
    ]
    times = {"client_ms": round(c_end - c_start), "payment_ms": round(p_end)}
    return spans, times


def test_payment_server_self_time_excludes_the_fraud_call() -> None:
    spans, t = _payment_fraud()
    span_self = compute_self_times(spans)
    service_self = compute_service_self_times(spans)
    ps = ("t1", "ps")
    # Span self-time subtracts both children (own_work 60 ms + CLIENT 338 ms).
    assert _ms(span_self[ps]) == pytest.approx(t["payment_ms"] - 60 - t["client_ms"])
    # Service self-time subtracts only the CLIENT interval (waiting for fraud, network included):
    # payment's own work inside the INTERNAL span stays in payment's self-time.
    assert _ms(service_self[ps]) == pytest.approx(t["payment_ms"] - t["client_ms"])
    assert t["payment_ms"] - t["client_ms"] == 162
    # fraud's SERVER span has no outbound calls: all of it is fraud's own time.
    assert _ms(service_self[("t1", "fs")]) == pytest.approx(336)


def test_slower_fraud_does_not_change_payment_service_self_time() -> None:
    base, _ = _payment_fraud()
    slow_fraud, _ = _payment_fraud(fraud_ms=336 + 250)
    slow_payment, _ = _payment_fraud(payment_own_ms=60 + 250)
    key = ("t1", "ps")
    before = compute_service_self_times(base)[key]
    assert compute_service_self_times(slow_fraud)[key] == before
    assert _ms(compute_service_self_times(slow_payment)[key] - before) == pytest.approx(250)
    # The total duration rises in both cases — only self-time tells them apart.
    assert slow_fraud[0].duration_ns > base[0].duration_ns
    assert slow_payment[0].duration_ns > base[0].duration_ns


def test_concurrent_calls_are_subtracted_once() -> None:
    spans = [
        make_span("os", None, "orders", S, 0, 300),
        make_span("ow", "os", "orders", IN, 1, 13),
        make_span("ci", "os", "orders", C, 14, 40),  # → inventory
        make_span("cp", "os", "orders", C, 14, 290),  # → payment, concurrently
    ]
    assert _ms(compute_service_self_times(spans)[("t1", "os")]) == pytest.approx(300 - 276)


def test_call_outliving_the_parent_is_clipped() -> None:
    # gather(): payment's call fails at 99 ms and orders answers at 100 ms while the inventory
    # call is still running until 140 ms.
    spans = [
        make_span("os", None, "orders", S, 0, 100, http_status=502),
        make_span("cp", "os", "orders", C, 14, 99, http_status=500),
        make_span("ci", "os", "orders", C, 14, 140),
    ]
    assert _ms(compute_service_self_times(spans)[("t1", "os")]) == pytest.approx(14)
    assert _ms(compute_self_times(spans)[("t1", "os")]) == pytest.approx(14)


def test_entries_subset_and_unknown_entry() -> None:
    spans, _ = _payment_fraud()
    idx = TraceIndex.build(spans)
    only = compute_service_self_times(idx, entries=[idx.by_key[("t1", "fs")]])
    assert set(only) == {("t1", "fs")}
    stranger = make_span("zz", None, "x", S, 0, 1)
    with pytest.raises(KeyError):
        compute_service_self_times(idx, entries=[stranger])


# ----------------------------------------------------------------------------- properties

_coord = st.integers(-20, 80)


def _interval() -> st.SearchStrategy[tuple[int, int]]:
    return st.tuples(_coord, _coord).map(lambda p: (min(p), max(p)))


def _span(sid: str, parent: str | None, service: str, kind: SpanKind, iv: tuple[int, int]) -> Span:
    return Span(
        tenant_id="t",
        trace_id="tr",
        span_id=sid,
        parent_span_id=parent,
        service=service,
        service_version=None,
        environment=None,
        name=sid,
        kind=kind,
        start_ns=iv[0],
        end_ns=iv[1],
    )


def _brute_covered(intervals: list[tuple[int, int]], lo: int, hi: int) -> int:
    """Oracle: count the unit cells [t, t+1) of [lo, hi) covered by any interval."""
    return sum(1 for t in range(lo, hi) if any(a <= t < b for a, b in intervals))


@settings(max_examples=300, deadline=None)
@given(
    parent=st.tuples(st.integers(0, 30), st.integers(30, 60)),
    children=st.lists(_interval(), max_size=6),
    grandchildren=st.lists(st.tuples(st.integers(0, 5), _interval()), max_size=6),
)
def test_span_self_time_properties(
    parent: tuple[int, int],
    children: list[tuple[int, int]],
    grandchildren: list[tuple[int, tuple[int, int]]],
) -> None:
    p = _span("p", None, "a", S, parent)
    kids = [_span(f"c{i}", "p", "a", C, iv) for i, iv in enumerate(children)]
    grand = [
        _span(f"g{j}", f"c{k % len(kids)}", "b", S, iv)
        for j, (k, iv) in enumerate(grandchildren)
        if kids
    ]
    result = compute_self_times([p, *kids, *grand])[("tr", "p")]
    duration = parent[1] - parent[0]

    # always within [0, duration]
    assert 0 <= result <= duration
    # union of clipped children counted once, whatever overlaps and overhangs (oracle)
    assert result == duration - _brute_covered(children, parent[0], parent[1])
    # grandchildren never change the parent's self-time
    assert result == self_time_ns(p, kids)
    if not children:
        assert result == duration


@settings(max_examples=200, deadline=None)
@given(parent=st.tuples(st.integers(0, 30), st.integers(30, 60)), data=st.data())
def test_single_child_inside_parent(parent: tuple[int, int], data: st.DataObject) -> None:
    lo = data.draw(st.integers(parent[0], parent[1]))
    hi = data.draw(st.integers(lo, parent[1]))
    p = _span("p", None, "a", S, parent)
    c = _span("c", "p", "a", IN, (lo, hi))
    assert compute_self_times([p, c])[("tr", "p")] == (parent[1] - parent[0]) - (hi - lo)


def _random_tree(draw: st.DrawFn) -> list[Span]:
    """A random trace below one SERVER entry of service ``a``; children may overhang."""
    entry = _span("n0", None, "a", S, (0, draw(st.integers(1, 60))))
    spans = [entry]
    for i in range(1, draw(st.integers(0, 10)) + 1):
        parent = spans[draw(st.integers(0, len(spans) - 1))]
        service = draw(st.sampled_from(["a", "a", "b"]))
        kind = draw(st.sampled_from([S, C, IN, SpanKind.PRODUCER, SpanKind.CONSUMER]))
        spans.append(_span(f"n{i}", parent.span_id, service, kind, draw(_interval())))
    return spans


def _naive_waits(spans: list[Span], entry: Span) -> list[tuple[int, int]]:
    """Independent recursive restatement of the service self-time wait set."""
    by_parent: dict[str, list[Span]] = {}
    for s in spans:
        if s.parent_span_id is not None:
            by_parent.setdefault(s.parent_span_id, []).append(s)

    def walk(node: Span) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for child in by_parent.get(node.span_id, []):
            if child.service != entry.service or child.kind in (C, SpanKind.PRODUCER):
                out.append((child.start_ns, child.end_ns))
            else:
                out.extend(walk(child))
        return out

    return walk(entry)


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_service_self_time_properties(data: st.DataObject) -> None:
    spans = data.draw(st.composite(_random_tree)())
    entry = spans[0]
    result = compute_service_self_times(spans)[entry.key]
    assert 0 <= result <= entry.duration_ns
    waits = _naive_waits(spans, entry)
    assert result == entry.duration_ns - _brute_covered(waits, entry.start_ns, entry.end_ns)
    if not waits:
        assert result == entry.duration_ns


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_service_self_time_at_least_span_self_time_when_nested(data: st.DataObject) -> None:
    """With well-nested intervals, waits are a subset of the direct children's coverage."""
    end = data.draw(st.integers(1, 60))
    spans = [_span("n0", None, "a", S, (0, end))]
    for i in range(1, data.draw(st.integers(0, 8)) + 1):
        parent = spans[data.draw(st.integers(0, len(spans) - 1))]
        lo = data.draw(st.integers(parent.start_ns, parent.end_ns))
        hi = data.draw(st.integers(lo, parent.end_ns))
        service = data.draw(st.sampled_from(["a", "b"]))
        kind = data.draw(st.sampled_from([C, IN, S]))
        spans.append(_span(f"n{i}", parent.span_id, service, kind, (lo, hi)))
    key = spans[0].key
    span_self = compute_self_times(spans)[key]
    service_self = compute_service_self_times(spans)[key]
    assert span_self <= service_self <= spans[0].duration_ns
    direct = [s for s in spans if s.parent_span_id == "n0"]
    if all(s.kind is C or s.service != "a" for s in direct):
        assert service_self == span_self
