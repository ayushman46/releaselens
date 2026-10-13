"""Edge increments and minute rollups derived from newly stored spans."""

from __future__ import annotations

import itertools
import math
import random

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tracegen import make_span

from rl_core.telemetry.model import NS_PER_MS, SpanKind
from rl_core.telemetry.rollup import (
    LATENCY_BUCKET_BOUNDS_MS,
    LATENCY_BUCKETS,
    StoredSpanRef,
    approx_quantile,
    bucket_index,
    bucket_range_ms,
    edge_increments,
    edge_increments_as_mapping,
    merge_histograms,
    minute_rollups,
)

S, C, INTERNAL = SpanKind.SERVER, SpanKind.CLIENT, SpanKind.INTERNAL


def _ref(span_id: str, parent: str | None, service: str, *, trace: str = "t1") -> StoredSpanRef:
    return StoredSpanRef(
        trace_id=trace,
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        environment="demo",
        start_ns=int(span_id, 16) * NS_PER_MS,
    )


# gateway(S 1) -> gateway(C 2) -> orders(S 3) -> orders(C 4) -> payment(S 5)
TRACE = [
    _ref("1", None, "gateway"),
    _ref("2", "1", "gateway"),
    _ref("3", "2", "orders"),
    _ref("4", "3", "orders"),
    _ref("5", "4", "payment"),
]
EXPECTED = {("demo", "gateway", "orders"): 1, ("demo", "orders", "payment"): 1}


def test_all_new_in_one_batch() -> None:
    inc = edge_increments(TRACE, {r.key for r in TRACE})
    assert edge_increments_as_mapping(inc) == EXPECTED
    assert inc[("demo", "orders", "payment")].first_seen_ns == 5 * NS_PER_MS


@pytest.mark.parametrize("seed", range(20))
def test_any_arrival_order_counts_each_call_exactly_once(seed: int) -> None:
    rng = random.Random(seed)
    order = TRACE[:]
    rng.shuffle(order)
    cut = sorted(rng.sample(range(1, len(order)), k=2))
    batches = [order[: cut[0]], order[cut[0] : cut[1]], order[cut[1] :]]
    stored: list[StoredSpanRef] = []
    total: dict[tuple[str, str, str], int] = {}
    for batch in batches:
        stored += batch
        inc = edge_increments(stored, {r.key for r in batch})
        for key, calls in edge_increments_as_mapping(inc).items():
            total[key] = total.get(key, 0) + calls
    assert total == EXPECTED


def test_redelivered_spans_add_nothing() -> None:
    assert edge_increments(TRACE, set()) == {}


def test_same_service_parent_child_is_not_an_edge_and_unknown_env_is_empty_string() -> None:
    refs = [
        StoredSpanRef("t", "1", None, "a", None, 0),
        StoredSpanRef("t", "2", "1", "a", None, 1),
        StoredSpanRef("t", "3", "2", "b", None, 2),
    ]
    assert edge_increments_as_mapping(edge_increments(refs, {r.key for r in refs})) == {
        ("", "a", "b"): 1
    }


def test_spans_of_another_trace_never_link() -> None:
    refs = [_ref("1", None, "a", trace="t1"), _ref("2", "1", "b", trace="t2")]
    assert edge_increments(refs, {r.key for r in refs}) == {}


# ----------------------------------------------------------------------------- histogram
def test_bucket_bounds() -> None:
    assert LATENCY_BUCKETS == 42
    assert LATENCY_BUCKET_BOUNDS_MS[0] == 0.125
    assert LATENCY_BUCKET_BOUNDS_MS[-1] == 131072.0
    ratios = {round(b / a, 9) for a, b in itertools.pairwise(LATENCY_BUCKET_BOUNDS_MS)}
    assert ratios == {round(math.sqrt(2), 9)}
    assert bucket_index(0) == 0
    assert bucket_index(0.125) == 0  # upper bounds are inclusive
    assert bucket_index(0.126) == 1
    assert bucket_index(1.0) == 6
    assert bucket_index(10**9) == LATENCY_BUCKETS - 1
    assert bucket_range_ms(LATENCY_BUCKETS - 1) == (131072.0, math.inf)
    with pytest.raises(ValueError):
        bucket_index(-1)


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.floats(min_value=0.0, max_value=100_000.0), min_size=1, max_size=400),
    st.sampled_from([0.5, 0.9, 0.95, 0.99]),
)
def test_approx_quantile_brackets_the_exact_value(values: list[float], q: float) -> None:
    hist = [0] * LATENCY_BUCKETS
    for v in values:
        hist[bucket_index(v)] += 1
    est = approx_quantile(hist, q)
    assert est is not None
    # The estimate's bucket contains the order statistic at rank ceil(q * n).
    exact = sorted(values)[max(0, math.ceil(q * len(values)) - 1)]
    assert est.low_ms <= exact <= est.high_ms or math.isclose(exact, est.low_ms)
    assert est.low_ms <= est.value_ms <= est.high_ms


def test_approx_quantile_is_close_on_lognormal_latencies() -> None:
    rng = np.random.default_rng(0)
    values = rng.lognormal(mean=math.log(60), sigma=0.25, size=5000)
    hist = [0] * LATENCY_BUCKETS
    for v in values:
        hist[bucket_index(float(v))] += 1
    est = approx_quantile(hist, 0.95)
    assert est is not None
    exact = float(np.quantile(values, 0.95))
    assert abs(est.value_ms / exact - 1) < 0.15  # measured: well inside one √2 bucket
    assert approx_quantile([0] * LATENCY_BUCKETS, 0.95) is None


def test_merge_is_addition() -> None:
    a = [1] * LATENCY_BUCKETS
    b = list(range(LATENCY_BUCKETS))
    assert merge_histograms(a, b) == [x + 1 for x in b]
    with pytest.raises(ValueError):
        merge_histograms([1], b)


def test_minute_rollups_use_server_spans_only() -> None:
    minute = 60_000.0
    spans = [
        make_span("1", None, "payment", S, 0, 61),
        make_span("2", "1", "payment", INTERNAL, 1, 60),
        make_span("3", None, "payment", S, 1000, 1200, error=True, trace_id="t2"),
        make_span("4", None, "payment", S, minute + 5, minute + 9, trace_id="t3"),
        make_span("5", None, "fraud", S, 0, 35, http_status=503, trace_id="t4"),
    ]
    roll = minute_rollups(spans)
    first = roll[("demo", "payment", 0)]
    assert (first.request_count, first.error_count) == (2, 1)
    assert first.duration_sum_ms == pytest.approx(261.0)
    assert first.duration_max_ms == pytest.approx(200.0)
    assert sum(first.buckets) == 2
    assert roll[("demo", "payment", 60 * 10**9)].request_count == 1
    assert roll[("demo", "fraud", 0)].error_count == 1  # HTTP 5xx on a SERVER span
