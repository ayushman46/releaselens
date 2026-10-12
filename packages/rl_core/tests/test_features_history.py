"""History-window correctness: hand-built timelines + randomized comparison with a naive oracle."""

from __future__ import annotations

import math
import statistics

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rl_core.features.history import History, history_features, max_input_finished_at

NAN = float("nan")


def H(rows: list[tuple[float, float, float, float]]) -> History:
    """rows: (started_at, finished_at, failed, duration)."""
    arr = np.array(rows, dtype=np.float64).reshape(-1, 4)
    return History(
        started_at=arr[:, 0], finished_at=arr[:, 1], failed=arr[:, 2], duration=arr[:, 3]
    )


def one(h: History, t: float) -> dict[str, float]:
    return {k: float(v[0]) for k, v in history_features(h, [t]).items()}


def same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


# ----------------------------------------------------------------------------- hand-built


def test_build_still_running_does_not_count() -> None:
    h = H([(0, 100, 1, 100)])  # A: started 0, finished 100, failed
    f = one(h, 50)  # B starts while A is running
    assert f["history_size"] == 0
    assert math.isnan(f["prev_build_failed"])
    assert f["hours_since_last_build"] == pytest.approx(50 / 3600)  # A's *start* is known
    f = one(h, 150)
    assert f["history_size"] == 1
    assert f["prev_build_failed"] == 1


def test_finish_exactly_at_query_time_is_excluded() -> None:
    h = H([(0, 100, 1, 100)])
    assert one(h, 100)["history_size"] == 0
    assert one(h, 100.000001)["history_size"] == 1


def test_previous_is_most_recently_finished_not_most_recently_started() -> None:
    # A starts first but finishes last; B starts later, finishes earlier.
    h = H([(0, 300, 1, 300), (10, 50, 0, 40)])
    f = one(h, 400)
    assert f["prev_build_failed"] == 1  # A finished at 300 > B at 50
    assert f["prev_build_duration"] == 300
    f = one(h, 100)  # only B finished
    assert f["prev_build_failed"] == 0
    assert f["history_size"] == 1


def test_builds_without_outcome_or_finish_never_count() -> None:
    h = H([(0, 10, NAN, 10), (20, NAN, 1, NAN), (30, 40, 0, 10)])  # canceled, unknown, passed
    f = one(h, 1000)
    assert f["history_size"] == 1
    assert f["prev_build_failed"] == 0
    assert f["fail_rate_last_5"] == 0


def test_fail_rates_and_builds_since_last_failure() -> None:
    # finished in order: F P P F P P P  (1=failed)
    seq = [1, 0, 0, 1, 0, 0, 0]
    h = H([(i * 10, i * 10 + 5, s, 5) for i, s in enumerate(seq)])
    f = one(h, 1000)
    assert f["history_size"] == 7
    assert f["fail_rate_last_5"] == pytest.approx(1 / 5)  # P P F P P P -> last5 = 0 1 0 0 0
    assert f["fail_rate_last_20"] == pytest.approx(2 / 7)
    assert f["builds_since_last_failure"] == 3
    assert f["prev_build_failed"] == 0


def test_builds_since_last_failure_is_nan_without_any_failure() -> None:
    h = H([(0, 5, 0, 5), (10, 15, 0, 5)])
    assert math.isnan(one(h, 100)["builds_since_last_failure"])
    assert one(H([(0, 5, 1, 5)]), 100)["builds_since_last_failure"] == 0


def test_duration_zscore() -> None:
    durs = [100, 110, 90, 105, 95, 300]
    h = H([(i * 1000, i * 1000 + d, 0, d) for i, d in enumerate(durs)])
    f = one(h, 10_000)
    mean, sd = statistics.mean(durs), statistics.stdev(durs)
    assert f["prev_build_duration"] == 300
    assert f["prev_duration_zscore_vs_project_last_20"] == pytest.approx((300 - mean) / sd)


def test_zscore_needs_three_samples_and_spread() -> None:
    h = H([(0, 10, 0, 10), (20, 30, 0, 10)])
    assert math.isnan(one(h, 100)["prev_duration_zscore_vs_project_last_20"])
    h = H([(i * 100, i * 100 + 10, 0, 10) for i in range(5)])  # zero spread
    assert math.isnan(one(h, 1000)["prev_duration_zscore_vs_project_last_20"])


def test_empty_history() -> None:
    f = one(History.empty(), 123.0)
    assert f["history_size"] == 0
    for k in ("prev_build_failed", "fail_rate_last_5", "hours_since_last_build"):
        assert math.isnan(f[k])


# ----------------------------------------------------------------------------- oracle


def naive(h: History, t: float) -> dict[str, float]:
    """Direct transcription of the spec's rule, O(n) per query, no cleverness."""
    rows = [
        (fin, fail, dur, i)
        for i, (fin, fail, dur) in enumerate(zip(h.finished_at, h.failed, h.duration, strict=True))
        if not math.isnan(fin) and not math.isnan(fail) and fin < t
    ]
    rows.sort(key=lambda r: (r[0], r[3]))
    fails = [r[1] for r in rows]
    durs = [r[2] for r in rows]
    out: dict[str, float] = {"history_size": float(len(rows))}
    out["prev_build_failed"] = fails[-1] if rows else NAN
    for k in (5, 20):
        w = fails[-k:]
        out[f"fail_rate_last_{k}"] = sum(w) / len(w) if w else NAN
    last_fail = max((i for i, f in enumerate(fails) if f > 0.5), default=None)
    out["builds_since_last_failure"] = (len(rows) - 1 - last_fail) if last_fail is not None else NAN
    out["prev_build_duration"] = durs[-1] if rows else NAN
    window = [d for d in durs[-20:] if not math.isnan(d)]
    prev = durs[-1] if rows else NAN
    if len(window) >= 3 and not math.isnan(prev) and statistics.stdev(window) > 1e-9:
        out["prev_duration_zscore_vs_project_last_20"] = (prev - statistics.mean(window)) / (
            statistics.stdev(window)
        )
    else:
        out["prev_duration_zscore_vs_project_last_20"] = NAN
    starts = [s for s in h.started_at if not math.isnan(s) and s < t]
    out["hours_since_last_build"] = (t - max(starts)) / 3600 if starts else NAN
    return out


timeline = st.lists(
    st.tuples(
        st.integers(0, 500),  # start
        st.one_of(st.integers(0, 200), st.just(-1)),  # duration (-1 → unknown finish)
        st.sampled_from([0.0, 1.0, NAN]),  # outcome (NaN → canceled)
    ),
    max_size=40,
)


@settings(max_examples=300, deadline=None)
@given(rows=timeline, queries=st.lists(st.integers(-10, 800), min_size=1, max_size=8))
def test_vectorised_matches_naive_oracle(
    rows: list[tuple[int, int, float]], queries: list[int]
) -> None:
    h = H(
        [
            (float(s), NAN if d < 0 else float(s + d), f, NAN if d < 0 else float(d))
            for s, d, f in rows
        ]
    )
    got = history_features(h, queries)
    for qi, t in enumerate(queries):
        want = naive(h, float(t))
        for name, expected in want.items():
            assert same(float(got[name][qi]), expected), (name, t, got[name][qi], expected)


@settings(max_examples=200, deadline=None)
@given(rows=timeline, queries=st.lists(st.integers(-10, 800), min_size=1, max_size=8))
def test_no_input_finished_at_or_after_query_time(
    rows: list[tuple[int, int, float]], queries: list[int]
) -> None:
    h = H(
        [
            (float(s), NAN if d < 0 else float(s + d), f, NAN if d < 0 else float(d))
            for s, d, f in rows
        ]
    )
    latest = max_input_finished_at(h, queries)
    for t, m in zip(queries, latest, strict=True):
        assert math.isnan(m) or m < t
