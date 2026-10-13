"""Leakage contract + offline (batch) vs online (per-request) parity — spec §8.3, §9."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from rl_core.features import FEATURE_NAMES, FORBIDDEN_INPUTS
from rl_core.features.change import BUILD_INPUT_FIELDS, time_features
from rl_core.features.offline import AUDIT_COLUMN, compute_features
from rl_core.features.online import PastRun, compute_online
from rl_core.features.schema import ALL_FEATURE_NAMES, HUMAN_LABELS


def test_feature_list_never_intersects_forbidden_inputs() -> None:
    assert set(ALL_FEATURE_NAMES).isdisjoint(FORBIDDEN_INPUTS)
    assert set(BUILD_INPUT_FIELDS).isdisjoint(FORBIDDEN_INPUTS)


def test_every_feature_has_a_human_label_and_names_are_unique() -> None:
    assert len(ALL_FEATURE_NAMES) == len(set(ALL_FEATURE_NAMES))
    assert set(ALL_FEATURE_NAMES) <= set(HUMAN_LABELS)
    assert set(FEATURE_NAMES) < set(ALL_FEATURE_NAMES)  # time features are opt-in


def test_time_features_known_instants() -> None:
    monday_midnight = datetime(2024, 1, 1, tzinfo=UTC).timestamp()  # 2024-01-01 was a Monday
    f = time_features([monday_midnight])
    assert f["hour_of_week_sin"][0] == pytest.approx(0.0, abs=1e-12)
    assert f["hour_of_week_cos"][0] == pytest.approx(1.0)
    thursday_noon = datetime(2024, 1, 4, 12, tzinfo=UTC).timestamp()  # hour-of-week 84 = half
    assert time_features([thursday_noon])["hour_of_week_cos"][0] == pytest.approx(-1.0)


def _random_builds(seed: int, n: int = 400) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    base = datetime(2015, 1, 1, tzinfo=UTC)
    starts = np.sort(rng.uniform(0, 90 * 86400, n))
    dur = rng.integers(30, 4 * 3600, n).astype(float)
    dur[rng.random(n) < 0.02] = np.nan  # unknown durations
    failed = (rng.random(n) < 0.3).astype(float)
    failed[rng.random(n) < 0.02] = np.nan  # canceled
    rows = {
        "build_id": np.arange(1, n + 1),
        "project": rng.choice(["a/one", "b/two", "c/three"], n),
        "started_at": [base + timedelta(seconds=float(s)) for s in starts],
        "finished_at": [
            None if math.isnan(d) else base + timedelta(seconds=float(s + d))
            for s, d in zip(starts, dur, strict=True)
        ],
        "failed": failed,
        "duration_s": dur,
    }
    for name in BUILD_INPUT_FIELDS:
        vals = rng.integers(0, 500, n).astype(float)
        vals[rng.random(n) < 0.1] = np.nan
        rows[name] = vals
    rows["is_pr"] = (rng.random(n) < 0.3).astype(float)
    df = pl.DataFrame(rows)
    return df.with_columns(pl.col("failed").fill_nan(None), pl.col("duration_s").fill_nan(None))


def test_offline_rows_never_use_builds_finishing_after_start() -> None:
    df = _random_builds(1)
    feats = compute_features(df).join(df.select("build_id", "started_at"), on="build_id")
    started = feats["started_at"].dt.epoch("us").cast(pl.Float64) / 1e6
    audit = feats[AUDIT_COLUMN]
    ok = audit.is_null() | (audit < started)
    assert bool(ok.all())


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_offline_and_online_produce_identical_vectors(seed: int) -> None:
    df = _random_builds(seed)
    offline = compute_features(df)
    by_id = {r["build_id"]: r for r in offline.iter_rows(named=True)}

    sample = df.sample(n=60, seed=seed)
    for row in sample.iter_rows(named=True):
        others = df.filter(
            (pl.col("project") == row["project"]) & (pl.col("build_id") != row["build_id"])
        )
        # Serving sees only what ReleaseLens has stored: every other run of the repo, with the
        # finish time it actually has. Unfinished runs are passed too — the function must ignore
        # them, exactly as offline does.
        history = [
            PastRun(
                started_at=o["started_at"],
                finished_at=o["finished_at"],
                failed=None if o["failed"] is None else bool(o["failed"]),
                duration_s=o["duration_s"],
            )
            for o in others.iter_rows(named=True)
        ]
        facts = {
            k: (None if row[k] is None or math.isnan(row[k]) else row[k])
            for k in BUILD_INPUT_FIELDS
        }
        online = compute_online(facts, history, row["started_at"])
        expected = by_id[row["build_id"]]
        for name in ALL_FEATURE_NAMES:
            a, b = online[name], expected[name]
            if a is None or b is None:
                assert a is None, (name, a, b)
                assert b is None, (name, a, b)
            else:
                assert a == pytest.approx(b, rel=1e-9, abs=1e-9), (name, a, b)


def test_online_rejects_unknown_facts_and_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="unknown change facts"):
        compute_online({"tr_status": 1}, [], datetime(2020, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_online({}, [], datetime(2020, 1, 1))


def test_offline_rejects_duplicate_builds() -> None:
    df = _random_builds(4, n=10)
    with pytest.raises(ValueError, match="unique"):
        compute_features(pl.concat([df, df.head(1)]))


# ----------------------------------------------------------------------------- bounded history
def _runs(seed: int, n: int) -> list[PastRun]:
    """Random runs with finish-time ties, canceled and still-running builds."""
    rng = np.random.default_rng(seed)
    base = datetime(2024, 1, 1, tzinfo=UTC)
    out = []
    for _ in range(n):
        start = base + timedelta(minutes=int(rng.integers(0, 60 * 24 * 30)))
        kind = rng.random()
        if kind < 0.05:  # still running
            out.append(PastRun(start, None, None, None))
            continue
        # coarse durations make equal finish times (ties) likely
        finish = start + timedelta(minutes=int(rng.integers(1, 4)) * 10)
        failed = None if kind < 0.1 else bool(rng.random() < 0.3)
        dur = None if rng.random() < 0.05 else (finish - start).total_seconds()
        out.append(PastRun(start, finish, failed, dur))
    return out


@pytest.mark.parametrize(
    ("seed", "n", "limit"), [(1, 300, 20), (2, 300, 25), (3, 15, 20), (4, 0, 20)]
)
def test_bounded_history_with_totals_equals_the_full_history(seed: int, n: int, limit: int) -> None:
    from rl_core.features.online import bounded_history

    runs = _runs(seed, n)
    rng = np.random.default_rng(seed + 100)
    base = datetime(2024, 1, 1, tzinfo=UTC)
    queries = [base + timedelta(minutes=int(m)) for m in rng.integers(0, 60 * 24 * 32, 40)]
    for t in queries:
        window, totals = bounded_history(runs, t, limit)
        assert len([r for r in window if r.finished_at is not None]) <= limit
        full = compute_online({"src_churn": 3}, runs, t)
        bounded = compute_online({"src_churn": 3}, window, t, totals=totals)
        assert bounded == full, t


def test_bounded_history_keeps_the_last_failure_beyond_the_window() -> None:
    from rl_core.features.online import bounded_history, history_totals

    base = datetime(2024, 1, 1, tzinfo=UTC)
    runs = [
        PastRun(base + timedelta(hours=i), base + timedelta(hours=i, minutes=5), i == 0, 300.0)
        for i in range(50)
    ]
    t = base + timedelta(days=5)
    totals = history_totals(runs, t)
    assert (totals.eligible, totals.since_last_failure) == (50, 49)
    window, _ = bounded_history(runs, t, 20)
    without = compute_online({}, window, t)  # the window alone has no failure in it
    with_totals = compute_online({}, window, t, totals=totals)
    assert without["builds_since_last_failure"] is None
    assert with_totals["builds_since_last_failure"] == 49.0
    assert with_totals["history_size"] == 50.0
    assert with_totals == compute_online({}, runs, t)


def test_history_totals_validation() -> None:
    from rl_core.features.online import HistoryTotals, bounded_history

    with pytest.raises(ValueError):
        HistoryTotals(eligible=-1, since_last_failure=None)
    with pytest.raises(ValueError):
        HistoryTotals(eligible=3, since_last_failure=3)
    with pytest.raises(ValueError, match="limit"):
        bounded_history([], datetime(2024, 1, 1, tzinfo=UTC), 5)
    empty = compute_online({}, [], datetime(2024, 1, 1, tzinfo=UTC), totals=HistoryTotals(0, None))
    assert empty["history_size"] == 0.0
    assert empty["builds_since_last_failure"] is None
