"""Explanations (SHAP) and drift monitoring (PSI)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from rl_core.features.schema import HUMAN_LABELS
from rl_ml import explain, monitoring
from rl_ml.models import XGBModel, _xgb, make_logreg


@pytest.fixture(scope="module")
def xgb_and_data():
    rng = np.random.default_rng(0)
    n = 2000
    X = pd.DataFrame(
        {
            "src_churn": rng.lognormal(3, 1.5, n),
            "log1p_src_churn": 0.0,
            "prev_build_failed": (rng.random(n) < 0.3).astype(float),
            "team_size": rng.integers(1, 30, n).astype(float),
        }
    )
    X["log1p_src_churn"] = np.log1p(X["src_churn"])
    X.loc[::17, "team_size"] = np.nan
    logit = -1.5 + 0.4 * (X["log1p_src_churn"] - 3) + 1.5 * X["prev_build_failed"]
    y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int).to_numpy()
    est = _xgb({"max_depth": 3, "learning_rate": 0.1}, spw=1.0, seed=0, n_estimators=50, early=None)
    est.fit(X, y)
    return XGBModel(est), X, y


def test_native_contributions_equal_shap_tree_explainer(xgb_and_data) -> None:
    import shap

    model, X, _ = xgb_and_data
    native, base = explain.contributions(model, X.head(200))
    sv = shap.TreeExplainer(model.estimator).shap_values(X.head(200))
    np.testing.assert_allclose(native, sv, atol=1e-4)
    margin = model.estimator.predict(X.head(200), output_margin=True)
    np.testing.assert_allclose(native.sum(axis=1) + base, margin, atol=1e-4)


def test_linear_contributions_reconstruct_the_logit(xgb_and_data) -> None:
    _, X, y = xgb_and_data
    from rl_ml.models import SklearnModel

    pipe = make_logreg(1.0, 0).fit(X, y)
    contrib, base = explain.contributions(SklearnModel(pipe), X.head(100))
    np.testing.assert_allclose(
        contrib.sum(axis=1) + base, pipe.decision_function(X.head(100)), atol=1e-8
    )


def test_explanations_group_shared_labels_and_use_training_percentiles(xgb_and_data) -> None:
    model, X, _ = xgb_and_data
    stats = explain.BackgroundStats.fit(X)
    row = X.copy().head(1)
    big = float(X["src_churn"].quantile(0.98))
    row.loc[row.index[0], ["src_churn", "log1p_src_churn"]] = [big, np.log1p(big)]
    contrib, _ = explain.contributions(model, row)
    ex = explain.explain_rows(contrib, row, stats)[0]
    items = ex["top_increasing"] + ex["top_reducing"]
    labels = [i["label"] for i in items]
    assert len(labels) == len(set(labels))  # src_churn + log1p_src_churn reported once
    churn = next(i for i in items if i["label"] == HUMAN_LABELS["src_churn"])
    assert churn["features"] == ["src_churn", "log1p_src_churn"]
    assert churn["contribution"] == pytest.approx(contrib[0, 0] + contrib[0, 1])
    assert "top" in churn["text"]
    assert "training data" in churn["text"]
    assert len(ex["top_increasing"]) <= 3
    assert len(ex["top_reducing"]) <= 3
    assert all(i["contribution"] > 0 for i in ex["top_increasing"])
    assert all(i["contribution"] < 0 for i in ex["top_reducing"])


def test_describe_value_texts() -> None:
    stats = explain.BackgroundStats.fit(
        pd.DataFrame({"x": np.arange(1000.0), "b": np.r_[np.zeros(500), np.ones(500)]})
    )
    fx, fb = stats.features["x"], stats.features["b"]
    assert (
        explain.describe_value("X", fx, 960.0)
        == "X is in the top 4% of builds in the training data"
    )
    assert (
        explain.describe_value("X", fx, 30.0)
        == "X is in the bottom 4% of builds in the training data"
    )
    assert (
        explain.describe_value("X", fx, 500.0)
        == "X is in the top 50% of builds in the training data"
    )
    assert (
        explain.describe_value("X", fx, 5000.0)
        == "X is higher than in any build in the training data"
    )
    assert explain.describe_value("X", fx, None) == "X is unknown for this build"
    assert explain.describe_value("B", fb, 1.0) == "B: yes"
    assert explain.describe_value("B", fb, 0.0) == "B: no"


def test_background_stats_round_trip(xgb_and_data) -> None:
    _, X, _ = xgb_and_data
    s = explain.BackgroundStats.fit(X)
    assert explain.BackgroundStats.from_dict(s.to_dict()) == s


def test_global_importance_ranks_the_signal(xgb_and_data) -> None:
    model, X, _ = xgb_and_data
    imp = explain.global_importance(model, X.head(500))
    top2 = {r["feature"] for r in imp[:2]}
    assert "prev_build_failed" in top2


# ---------------------------------------------------------------- PSI
def test_psi_is_near_zero_for_the_training_distribution() -> None:
    rng = np.random.default_rng(1)
    train = rng.normal(size=20_000)
    bins = monitoring.fit_bins(train)
    res = monitoring.compute_psi(bins, rng.normal(size=5000))
    assert res["psi"] < 0.02
    assert res["status"] == "stable"


def test_psi_flags_a_shift_and_missing_values() -> None:
    rng = np.random.default_rng(2)
    bins = monitoring.fit_bins(rng.normal(size=20_000))
    assert monitoring.compute_psi(bins, rng.normal(1.0, 1.0, 5000))["status"] == "major"
    live = rng.normal(size=5000)
    live[:1500] = np.nan
    assert monitoring.compute_psi(bins.to_dict(), live)["psi"] > 0.25


def test_psi_handles_heavily_tied_features() -> None:
    v = np.r_[np.zeros(7000), np.arange(3000.0)]
    bins = monitoring.fit_bins(v)
    assert len(bins.edges) < 9  # duplicate edges collapsed
    assert sum(bins.expected) == pytest.approx(1.0)
    assert monitoring.compute_psi(bins, v)["psi"] == pytest.approx(0.0, abs=1e-9)
