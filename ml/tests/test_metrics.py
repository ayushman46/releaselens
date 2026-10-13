"""Evaluation metrics, calibration (ADR-0012 gate) and risk bands on controlled inputs."""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, f1_score

from rl_ml import bands, calibrate, evaluate


def _calibrated(n: int = 20_000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    p = rng.beta(2, 5, size=n)
    y = (rng.random(n) < p).astype(int)
    return y, p


# ---------------------------------------------------------------- evaluate
def test_pr_auc_is_average_precision() -> None:
    y, p = _calibrated(2000)
    assert evaluate.pr_auc(y, p) == pytest.approx(average_precision_score(y, p))


def test_constant_score_pr_auc_equals_base_rate() -> None:
    y, _ = _calibrated(5000)
    assert evaluate.pr_auc(y, np.full(len(y), 0.3)) == pytest.approx(y.mean())


def test_best_f1_threshold_matches_brute_force() -> None:
    y, p = _calibrated(3000, seed=3)
    t = evaluate.best_f1_threshold(y, p)
    best = max(f1_score(y, p >= c) for c in np.unique(p))
    assert f1_score(y, p >= t) == pytest.approx(best)


def test_threshold_metrics_confusion_matrix() -> None:
    y = np.array([1, 1, 0, 0, 1, 0])
    s = np.array([0.9, 0.2, 0.8, 0.1, 0.7, 0.3])
    m = evaluate.threshold_metrics(y, s, 0.5)
    assert m["confusion_matrix"] == {"tp": 2, "fp": 1, "fn": 1, "tn": 2}
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)


def test_precision_and_lift_at_top() -> None:
    y = np.array([1] * 10 + [0] * 90)
    s = np.r_[np.linspace(1, 0.9, 10), np.linspace(0.5, 0, 90)]
    assert evaluate.precision_at_top(y, s, 0.1) == 1.0
    assert evaluate.lift_at_top(y, s, 0.1) == pytest.approx(10.0)


def test_ece_and_slope_on_calibrated_and_overconfident_scores() -> None:
    y, p = _calibrated()
    assert evaluate.expected_calibration_error(y, p) < 0.02
    cs = evaluate.calibration_slope(y, p)
    assert cs["slope"] == pytest.approx(1.0, abs=0.08)
    # overconfident: logit doubled → slope ~ 0.5
    z = 2 * evaluate.logit(p)
    over = 1 / (1 + np.exp(-z))
    assert evaluate.calibration_slope(y, over)["slope"] == pytest.approx(0.5, abs=0.06)
    assert evaluate.expected_calibration_error(y, over) > 0.05


def test_brier_skill_score_reference() -> None:
    y, p = _calibrated()
    assert evaluate.brier_skill_score(y, p) > 0
    assert evaluate.brier_skill_score(y, np.full(len(y), y.mean())) == pytest.approx(0.0, abs=1e-12)


def test_per_project_requires_both_classes_and_min_builds() -> None:
    y = np.array([1, 0, 1, 0] * 10 + [1] * 40 + [0, 1])
    s = np.linspace(0, 1, len(y))
    proj = np.array(["a"] * 40 + ["b"] * 40 + ["c"] * 2)
    res = evaluate.per_project_pr_auc(y, s, proj, min_builds=30)
    assert [r["project"] for r in res["projects"]] == ["a"]  # b: no negatives, c: too small
    assert res["summary"]["projects_total"] == 3


def test_cluster_bootstrap_ci_brackets_the_estimate() -> None:
    y, p = _calibrated(4000, seed=5)
    groups = np.repeat(np.arange(40), 100)
    ci = evaluate.cluster_bootstrap_ci(y, p, groups, n_resamples=100, seed=1)
    est = evaluate.pr_auc(y, p)
    assert ci["low"] <= est <= ci["high"]
    assert ci == evaluate.cluster_bootstrap_ci(y, p, groups, n_resamples=100, seed=1)


# ---------------------------------------------------------------- calibrate
def test_isotonic_calibrator_matches_sklearn() -> None:
    y, p = _calibrated(3000, seed=2)
    raw = p**2  # monotone distortion
    cal = calibrate.fit_isotonic(raw, y)
    ref = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(raw, y)
    grid = np.linspace(-0.1, 1.1, 301)
    np.testing.assert_allclose(cal.predict(grid), ref.predict(grid), atol=1e-12)


def test_platt_recovers_a_known_logistic_map() -> None:
    rng = np.random.default_rng(4)
    raw = rng.uniform(0.01, 0.99, 50_000)
    true_p = 1 / (1 + np.exp(-(-0.5 + 0.7 * evaluate.logit(raw))))
    y = (rng.random(raw.size) < true_p).astype(int)
    cal = calibrate.fit_platt(raw, y)
    assert cal.a == pytest.approx(-0.5, abs=0.05)
    assert cal.b == pytest.approx(0.7, abs=0.05)


def test_calibrator_json_round_trip() -> None:
    y, p = _calibrated(1000)
    for cal in (calibrate.fit_isotonic(p, y), calibrate.fit_platt(p, y)):
        again = calibrate.Calibrator.from_dict(cal.to_dict())
        np.testing.assert_allclose(again.predict(p), cal.predict(p))


def test_gate_passes_calibrated_and_fails_miscalibrated() -> None:
    y, p = _calibrated()
    ok = calibrate.gate(y, p)
    assert ok["passed"]
    assert all(ok["checks"].values())
    inflated = np.clip(p * 1.8, 0, 0.999)  # what class weighting does to raw scores
    bad = calibrate.gate(y, inflated)
    assert not bad["passed"]
    assert not bad["checks"]["ece_le_0.05"]


def test_method_selection_uses_only_given_data_and_is_deterministic() -> None:
    y, p = _calibrated(3000, seed=8)
    a = calibrate.select_method(p, y, seed=3)
    assert a == calibrate.select_method(p, y, seed=3)
    assert a["selected"] in {"isotonic", "platt"}


# ---------------------------------------------------------------- bands
def test_threshold_for_recall_is_the_highest_reaching_target() -> None:
    y, p = _calibrated(5000, seed=9)
    for target in (0.8, 0.5, 0.2):
        t = bands.threshold_for_recall(y, p, target)
        rec = y[p >= t].sum() / y.sum()
        assert rec >= target
        higher = p[p > t]
        if higher.size:
            t2 = higher.min()
            assert y[p >= t2].sum() / y.sum() < target


def test_band_cuts_are_ordered_and_banding_is_monotone() -> None:
    y, p = _calibrated(5000, seed=10)
    cuts = bands.BandCuts.fit(y, p)
    assert cuts.medium <= cuts.high <= cuts.critical
    order = {b: i for i, b in enumerate(bands.BANDS)}
    s = np.sort(p)
    labels = [order[b] for b in cuts.band(s)]
    assert labels == sorted(labels)
    rep = bands.band_report(y, p, cuts)
    assert rep["at_or_above"]["MEDIUM"]["recall"] >= 0.8
    assert rep["at_or_above"]["CRITICAL"]["precision"] > rep["base_rate"]
    assert bands.BandCuts.from_dict(cuts.as_dict()) == cuts


def test_risk_score_is_validation_percentile() -> None:
    val = np.arange(1000) / 1000.0
    scale = bands.ScoreScale.fit(val)
    assert scale.risk_score([-1.0])[0] == 0
    assert scale.risk_score([2.0])[0] == 100
    assert scale.risk_score([0.5])[0] == 50
    assert scale.risk_score([0.9])[0] == 90
    scores = scale.risk_score(np.random.default_rng(0).uniform(0, 1, 100))
    assert scores.min() >= 0
    assert scores.max() <= 100
