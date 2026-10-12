"""Leakage tests (spec section 8.3). Run by ``rl_ml.registry.promote`` before any promotion.

1. The model's feature lists never intersect ``FORBIDDEN_INPUTS``.
2. History features ignore builds that had not finished at the query time, on hand-built
   timelines, through the same offline (batch) and online (per-request) code the mart and the API
   use.
3. On the real feature mart, every row's ``max_history_input_finished_at < started_at``
   (skipped with a reason when the mart is not reachable).
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest

from rl_core.features.offline import compute_features
from rl_core.features.online import PastRun, compute_online
from rl_core.features.schema import (
    ALL_FEATURE_NAMES,
    FEATURE_NAMES,
    FORBIDDEN_INPUTS,
    HISTORY_FEATURES,
)
from rl_ml.data import LeakageError, leakage_audit, prepare_mart
from rl_ml.models import DEFAULT_TRIALS  # noqa: F401  (import check: rl_ml imports cleanly)

T0 = datetime(2014, 3, 1, 12, 0, tzinfo=UTC)


def _ts(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------- 1. forbidden inputs
def test_feature_lists_exclude_forbidden_inputs() -> None:
    assert set(FEATURE_NAMES) & FORBIDDEN_INPUTS == set()
    assert set(ALL_FEATURE_NAMES) & FORBIDDEN_INPUTS == set()


def test_label_and_status_are_forbidden() -> None:
    for name in ("label", "status", "tr_status", "duration", "tr_duration", "finished_at"):
        assert name in FORBIDDEN_INPUTS


def test_mart_refuses_forbidden_columns_as_features(synthetic_mart) -> None:
    with pytest.raises(LeakageError):
        synthetic_mart.X(["src_churn", "label"])
    with pytest.raises(LeakageError):
        leakage_audit(synthetic_mart, [*FEATURE_NAMES, "status"])


def test_trained_models_use_only_schema_features(trained_run) -> None:
    for cand in trained_run.outcome.candidates:
        assert set(cand.features) <= set(ALL_FEATURE_NAMES)
        assert set(cand.features) & FORBIDDEN_INPUTS == set()
    assert set(trained_run.bundle.features) & FORBIDDEN_INPUTS == set()


# ---------------------------------------------------------------- 2. hand-built timelines
def _timeline() -> pl.DataFrame:
    """Project P: A fails (0→30 min), B starts at 10 while A runs and passes at 20,
    C starts at 25 (A still running, B finished), D starts at 31 (A finished at 30),
    E starts at 35 while canceled X (no outcome) finished at 33."""
    rows = [
        # build_id, start, finish, failed
        (1, 0.0, 30.0, 1),  # A
        (2, 10.0, 20.0, 0),  # B
        (3, 25.0, 40.0, 1),  # C
        (4, 31.0, 50.0, 0),  # D
        (5, 32.0, 33.0, None),  # X canceled: no outcome
        (6, 35.0, 60.0, 0),  # E
    ]
    return pl.DataFrame(
        {
            "build_id": [r[0] for r in rows],
            "project": ["P"] * len(rows),
            "started_at": [_ts(r[1]) for r in rows],
            "finished_at": [_ts(r[2]) for r in rows],
            "failed": [r[3] for r in rows],
            "duration_s": [(r[2] - r[1]) * 60.0 for r in rows],
            "src_churn": [10.0] * len(rows),
        },
        schema_overrides={"failed": pl.Int64},
    )


def _offline() -> dict[int, dict[str, float]]:
    feats = compute_features(_timeline()).fill_null(float("nan"))
    return {int(r["build_id"]): r for r in feats.iter_rows(named=True)}


def test_running_build_is_ignored_by_history() -> None:
    f = _offline()
    # B starts while A is running: no finished history at all
    assert f[2]["history_size"] == 0
    assert math.isnan(f[2]["prev_build_failed"])
    # C starts at 25: only B (finished 20, passed) counts; A (finishes 30) does not
    assert f[3]["history_size"] == 1
    assert f[3]["prev_build_failed"] == 0
    assert f[3]["fail_rate_last_5"] == 0
    # D starts at 31: A finished at 30 (failed) is now the most recent finished build
    assert f[4]["history_size"] == 2
    assert f[4]["prev_build_failed"] == 1
    assert f[4]["fail_rate_last_5"] == pytest.approx(0.5)


def test_builds_without_outcome_never_count() -> None:
    f = _offline()
    # E starts at 35: A, B finished; canceled X finished at 33 but has no outcome; C, D running
    assert f[6]["history_size"] == 2
    assert f[6]["prev_build_failed"] == 1  # A (finished 30) — not X


def test_audit_column_is_strictly_before_start() -> None:
    feats = compute_features(_timeline())
    started = {
        int(b): s.timestamp()
        for b, s in zip(_timeline()["build_id"], _timeline()["started_at"], strict=True)
    }
    for row in feats.iter_rows(named=True):
        audit = row["max_history_input_finished_at"]
        if audit is not None and not math.isnan(audit):
            assert audit < started[int(row["build_id"])]


def test_online_path_matches_offline_on_the_same_timeline() -> None:
    tl = _timeline()
    offline = _offline()
    runs = [
        PastRun(
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            failed=None if r["failed"] is None else bool(r["failed"]),
            duration_s=r["duration_s"],
        )
        for r in tl.iter_rows(named=True)
    ]
    for r in tl.iter_rows(named=True):
        # serving sees the whole stored history; the finished-before rule must do the filtering
        online = compute_online({"src_churn": 10.0}, runs, r["started_at"])
        for name in HISTORY_FEATURES:
            a, b = online[name], offline[int(r["build_id"])][name]
            a = float("nan") if a is None else a
            assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b), (r["build_id"], name)


def test_leakage_audit_rejects_a_row_using_an_unfinished_build(synthetic_mart) -> None:
    df = synthetic_mart.frame.copy()
    i = int(np.flatnonzero(df["max_history_input_finished_at"].notna().to_numpy())[0])
    started = (df.loc[i, "started_at"] - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)
    df.loc[i, "max_history_input_finished_at"] = started + 1.0
    bad = prepare_mart(df, run_id="bad", source="synthetic", dataset_md5=None)
    with pytest.raises(LeakageError):
        leakage_audit(bad)


def test_synthetic_mart_passes_the_audit(synthetic_mart) -> None:
    audit = leakage_audit(synthetic_mart)
    assert audit["violations"] == 0
    assert audit["rows_with_history"] > 0


# ---------------------------------------------------------------- 3. the real mart
def _real_mart():
    from rl_ml.data import list_feature_runs, load_mart, make_s3_client

    try:
        client = make_s3_client()
        runs = list_feature_runs(client)
    except Exception as exc:  # object store not running
        pytest.skip(f"object store unreachable: {type(exc).__name__}")
    run_id = os.environ.get("RL_FEATURES_RUN_ID") or (runs[-1] if runs else None)
    if not run_id:
        pytest.skip(
            "feature mart not produced yet (no features/release_features/schema=1.0.0/run_id=*)"
        )
    return load_mart(run_id, client=client)


def test_real_mart_history_strictly_before_start() -> None:
    mart = _real_mart()
    audit = leakage_audit(mart)  # raises LeakageError on any violation
    assert audit["violations"] == 0
    assert audit["rows"] > 0


def test_real_mart_has_no_forbidden_columns_in_feature_set() -> None:
    mart = _real_mart()
    assert set(ALL_FEATURE_NAMES) <= set(mart.frame.columns)
    assert set(mart.frame["label"].unique()) <= {0, 1}
