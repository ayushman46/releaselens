import numpy as np
import pytest
from tracegen import make_span

from rl_core.regression.metrics import (
    ServiceSamples,
    build_service_samples,
    compute_window_stats,
    quantile,
    window_stats,
)
from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind

C, S, IN = SpanKind.CLIENT, SpanKind.SERVER, SpanKind.INTERNAL
MS = NS_PER_MS


def _requests() -> list[Span]:
    return [
        # svc requests at 0, 10, 20, 30 ms (durations 5, 7, 9, 11)
        make_span("r0", None, "svc", S, 0, 5, trace_id="a"),
        make_span("r1", None, "svc", S, 10, 17, trace_id="b", http_status=500),
        make_span("r2", None, "svc", S, 20, 29, trace_id="c"),
        make_span("r3", None, "svc", S, 30, 41, trace_id="d"),
        # non-SERVER spans never count as requests, errors included
        make_span("i0", "r0", "svc", IN, 1, 2, trace_id="a", error=True),
        make_span("c2", "r2", "svc", C, 21, 25, trace_id="c", http_status=500),
        make_span("o0", None, "other", S, 12, 13, trace_id="e"),
    ]


def test_window_membership_by_start_time() -> None:
    stats = compute_window_stats(_requests(), 10 * MS, 30 * MS)
    svc = stats["svc"]
    assert svc.request_count == 2  # r1, r2 (r3 starts exactly at the exclusive end)
    inclusive = compute_window_stats(_requests(), 10 * MS, 30 * MS, end_inclusive=True)
    assert inclusive["svc"].request_count == 3
    assert set(stats) == {"other", "svc"}


def test_counts_rates_and_throughput() -> None:
    stats = compute_window_stats(_requests(), 0, 60_000 * MS)["svc"]  # one minute
    assert stats.request_count == 4
    assert stats.error_count == 1  # r1 (SERVER 500); INTERNAL/CLIENT spans are not requests
    assert stats.error_rate == pytest.approx(0.25)
    assert stats.own_error_count == 1
    assert stats.duration_minutes == pytest.approx(1.0)
    assert stats.throughput_rpm == pytest.approx(4.0)
    np.testing.assert_allclose(stats.latency_ms, [5, 7, 9, 11])
    # r2 waited 4 ms on its CLIENT call; r0's INTERNAL work stays in its self-time
    np.testing.assert_allclose(stats.self_time_ms, [5, 7, 5, 11])


def test_percentiles_use_numpy_linear_method() -> None:
    stats = compute_window_stats(_requests(), 0, 60_000 * MS)["svc"]
    # linear interpolation between order statistics: p50 of [5,7,9,11] = 8.0,
    # p95 = 9 + 0.85*2 at rank 2.85 → 10.7
    assert stats.p50 == pytest.approx(8.0)
    assert stats.p95 == pytest.approx(10.7)
    assert stats.p99 == pytest.approx(10.94)
    assert stats.self_time_quantile(0.5) == pytest.approx(6.0)


def test_empty_window() -> None:
    empty = window_stats(ServiceSamples.empty("svc"), 0, 0, end_inclusive=True)
    assert empty.request_count == 0
    assert empty.error_rate is None
    assert empty.throughput_rpm is None  # zero-length window
    assert empty.p95 is None
    assert quantile(np.empty(0), 0.5) is None


def test_samples_sorted_and_select_bounds() -> None:
    spans = list(reversed(_requests()))
    samples = build_service_samples(TraceIndex.build(spans))["svc"]
    assert list(samples.start_ns) == sorted(samples.start_ns)
    assert len(samples.select(10 * MS, 20 * MS, hi_inclusive=False)) == 1
    assert len(samples.select(10 * MS, 20 * MS, hi_inclusive=True)) == 2
    assert len(samples.select(50 * MS, 10 * MS, hi_inclusive=True)) == 0
