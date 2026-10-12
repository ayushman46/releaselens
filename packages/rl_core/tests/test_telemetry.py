from datetime import UTC, datetime, timedelta, timezone

import pytest
from tracegen import make_span

from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import (
    NS_PER_MS,
    Span,
    SpanKind,
    StatusCode,
    datetime_to_ns,
    ns_to_datetime,
)

C, S, IN, P = SpanKind.CLIENT, SpanKind.SERVER, SpanKind.INTERNAL, SpanKind.PRODUCER


# ----------------------------------------------------------------------------- Span


def test_span_duration_properties() -> None:
    s = make_span("a", None, "svc", S, 10.0, 35.5)
    assert s.duration_ns == round(25.5 * NS_PER_MS)
    assert s.duration_ms == pytest.approx(25.5)
    assert s.key == ("t1", "a")
    assert s.parent_key is None
    assert make_span("b", "a", "svc", IN, 11, 12).parent_key == ("t1", "a")


@pytest.mark.parametrize(
    ("kind", "status", "http", "expected"),
    [
        (S, StatusCode.UNSET, 200, False),
        (S, StatusCode.UNSET, 500, True),  # SERVER 5xx is an error even without status
        (S, StatusCode.UNSET, 503, True),
        (S, StatusCode.UNSET, 404, False),  # 4xx is the client's fault
        (S, StatusCode.ERROR, 200, True),
        (C, StatusCode.UNSET, 500, False),  # CLIENT 5xx belongs to the callee
        (C, StatusCode.ERROR, 500, True),
        (IN, StatusCode.ERROR, None, True),
        (IN, StatusCode.OK, None, False),
    ],
)
def test_is_error(kind: SpanKind, status: StatusCode, http: int | None, expected: bool) -> None:
    s = Span(
        tenant_id="t",
        trace_id="tr",
        span_id="s",
        parent_span_id=None,
        service="svc",
        service_version=None,
        environment=None,
        name="n",
        kind=kind,
        start_ns=0,
        end_ns=1,
        status_code=status,
        http_status=http,
    )
    assert s.is_error is expected


def test_span_rejects_impossible_structure() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        make_span("a", None, "svc", S, 10, 5)
    with pytest.raises(ValueError, match="own parent"):
        make_span("a", "a", "svc", S, 1, 5)
    with pytest.raises(ValueError, match="non-empty"):
        make_span("", None, "svc", S, 1, 5)


def test_span_is_hashable_and_immutable() -> None:
    s = make_span("a", None, "svc", S, 1, 5)
    assert hash(s) == hash(make_span("a", None, "svc", S, 1, 5))
    with pytest.raises(AttributeError):
        s.start_ns = 0


# ----------------------------------------------------------------------------- time helpers


def test_datetime_ns_roundtrip_is_exact() -> None:
    dt = datetime(2026, 10, 13, 14, 2, 3, 123456, tzinfo=UTC)
    ns = datetime_to_ns(dt)
    assert ns == 1_791_900_123_123_456_000
    assert ns_to_datetime(ns) == dt
    # other offsets are converted, not misread
    assert datetime_to_ns(dt.astimezone(timezone(timedelta(hours=5, minutes=30)))) == ns


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        datetime_to_ns(datetime(2026, 1, 1))


# ----------------------------------------------------------------------------- TraceIndex


def _payment_trace() -> list[Span]:
    """payment SERVER → {own_work INTERNAL, CLIENT → fraud SERVER → fraud own_work}."""
    return [
        make_span("ps", None, "payment", S, 0, 500),
        make_span("pw", "ps", "payment", IN, 1, 61),
        make_span("pc", "ps", "payment", C, 62, 400),
        make_span("fs", "pc", "fraud", S, 63, 399),
        make_span("fw", "fs", "fraud", IN, 64, 398),
    ]


def test_index_children_and_parent() -> None:
    idx = TraceIndex.build(_payment_trace())
    ps = idx.by_key[("t1", "ps")]
    assert {c.span_id for c in idx.children_of(ps)} == {"pw", "pc"}
    assert idx.parent_of(idx.by_key[("t1", "fs")]) == idx.by_key[("t1", "pc")]
    assert idx.parent_of(ps) is None
    assert len(idx) == 5


def test_index_keys_on_trace_and_span_id() -> None:
    # Same span id in two traces: two distinct spans, children never cross traces.
    spans = [
        make_span("x", None, "a", S, 0, 10, trace_id="t1"),
        make_span("x", None, "a", S, 0, 10, trace_id="t2"),
        make_span("y", "x", "a", IN, 1, 5, trace_id="t2"),
    ]
    idx = TraceIndex.build(spans)
    assert len(idx) == 3
    assert idx.children_of(idx.by_key[("t1", "x")]) == ()
    assert len(idx.children_of(idx.by_key[("t2", "x")])) == 1


def test_index_drops_duplicates_keeping_first() -> None:
    first = make_span("a", None, "svc", S, 0, 10)
    replay = make_span("a", None, "svc", S, 0, 99)
    idx = TraceIndex.build([first, replay, first])
    assert idx.duplicates_dropped == 2
    assert idx.by_key[("t1", "a")] == first


def test_outbound_waits_stop_at_client_and_descend_through_internal() -> None:
    spans = [
        *_payment_trace(),
        # a CLIENT call made from inside the INTERNAL own_work span
        make_span("pc2", "pw", "payment", C, 10, 20),
        # an INTERNAL child below the CLIENT span: covered by the CLIENT interval, not visited
        make_span("pci", "pc", "payment", IN, 70, 80),
    ]
    idx = TraceIndex.build(spans)
    waits = {w.span_id for w in idx.outbound_waits(idx.by_key[("t1", "ps")])}
    assert waits == {"pc", "pc2"}


def test_outbound_waits_include_remote_child_without_client_span() -> None:
    spans = [
        make_span("as", None, "a", S, 0, 100),
        make_span("bs", "as", "b", S, 10, 50),  # context propagated, no CLIENT span recorded
        make_span("pr", "as", "a", P, 60, 61),
    ]
    idx = TraceIndex.build(spans)
    assert {w.span_id for w in idx.outbound_waits(idx.by_key[("t1", "as")])} == {"bs", "pr"}


def test_outbound_waits_survive_parent_cycles() -> None:
    # Malformed: a ↔ b parent cycle below the entry; the walk must terminate.
    spans = [
        make_span("e", None, "a", S, 0, 100),
        make_span("x", "y", "a", IN, 1, 2),
        make_span("y", "x", "a", IN, 3, 4),
        make_span("z", "e", "a", IN, 5, 6),
    ]
    idx = TraceIndex.build(spans)
    assert idx.outbound_waits(idx.by_key[("t1", "e")]) == []
    assert idx.boundary_spans(idx.by_key[("t1", "x")]) == []


def test_own_error_attribution() -> None:
    # payment fails in its own work: own error.
    own = [
        make_span("ps", None, "payment", S, 0, 100, http_status=500),
        make_span("pw", "ps", "payment", IN, 1, 50, error=True),
    ]
    idx = TraceIndex.build(own)
    assert idx.is_own_error(idx.by_key[("t1", "ps")])

    # orders fails because payment failed: not orders' own error (CLIENT span not marked ERROR,
    # only carries the callee's status — attribution must look through it).
    propagated = [
        make_span("os", None, "orders", S, 0, 100, http_status=502),
        make_span("oc", "os", "orders", C, 5, 90, http_status=500),
        make_span("ps", "oc", "payment", S, 6, 89, http_status=500),
    ]
    idx = TraceIndex.build(propagated)
    assert not idx.is_own_error(idx.by_key[("t1", "os")])
    assert idx.is_own_error(idx.by_key[("t1", "ps")])

    # callee answered fine (it handled its own callee's failure): the caller's error is its own.
    handled = [
        make_span("os", None, "orders", S, 0, 100, http_status=500),
        make_span("oc", "os", "orders", C, 5, 90, http_status=200),
        make_span("ps", "oc", "payment", S, 6, 89, http_status=200),
        make_span("pc", "ps", "payment", C, 7, 80, error=True),
        make_span("fs", "pc", "fraud", S, 8, 79, http_status=500),
    ]
    idx = TraceIndex.build(handled)
    assert idx.is_own_error(idx.by_key[("t1", "os")])

    # outbound call failed without reaching an instrumented callee: the caller's own error.
    timeout = [
        make_span("os", None, "orders", S, 0, 100, http_status=504),
        make_span("oc", "os", "orders", C, 5, 99, error=True),
    ]
    idx = TraceIndex.build(timeout)
    assert idx.is_own_error(idx.by_key[("t1", "os")])

    ok = make_span("os", None, "orders", S, 0, 100, http_status=200)
    assert not TraceIndex.build([ok]).is_own_error(ok)
