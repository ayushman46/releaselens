"""The no-history (cold-start) serving model and the bundle's fast serving path.

SYNTHETIC data only (rl_ml.synthetic): these numbers are never results.
"""

from __future__ import annotations

import copy
import math

import numpy as np
import pandas as pd
import pytest

from rl_core.features.schema import HISTORY_FEATURES
from rl_ml import cold
from rl_ml.explain import contributions
from rl_ml.predictor import RiskModelBundle


@pytest.fixture(scope="module")
def xgb_champion_results(trained_run):
    """The synthetic run's results, with XGBoost as the 'champion' (whatever won on synthetic)."""
    res = copy.deepcopy(trained_run.results)
    res["selection"]["final_model"] = "xgboost"
    res["selection"]["features"] = list(trained_run.outcome.by_name("xgboost").features)
    return res


@pytest.fixture(scope="module")
def cold_run(synthetic_mart, xgb_champion_results):
    return cold.run_cold_training(synthetic_mart, xgb_champion_results, seed=11)


def test_cold_model_has_no_history_features_and_the_champions_params(
    cold_run, xgb_champion_results
) -> None:
    feats = cold_run.results["model"]["features"]
    assert not set(feats) & set(HISTORY_FEATURES)
    assert set(feats) < set(xgb_champion_results["selection"]["features"])
    champ = next(c for c in xgb_champion_results["candidates"] if c["name"] == "xgboost")
    for key in ("max_depth", "learning_rate", "subsample", "colsample_bytree"):
        assert cold_run.results["model"]["params"][key] == champ["params"][key]
    assert cold_run.results["test"]["evaluations"] == 1
    assert cold_run.results["data"]["synthetic"] is True
    assert cold_run.bundle.meta["role"] == "cold_start"


def test_cold_bundle_round_trips_and_scores_rows_without_history(cold_run, tmp_path) -> None:
    paths = cold.write_outputs(cold_run, tmp_path)
    bundle = RiskModelBundle.load(paths["bundle"])
    out = bundle.assess([{"src_churn": 50, "is_pr": 1}], full_contributions=True)[0]
    assert 0 <= out["risk_score"] <= 100
    assert out["risk_band"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert set(out["contributions"]) == set(bundle.features)
    margin = out["base_value"] + sum(out["contributions"].values())
    assert 1 / (1 + math.exp(-margin)) == pytest.approx(out["raw_score"], abs=1e-5)
    if not cold_run.results["test"]["probability_exposed"]:
        assert out["probability"] is None


def test_promotion_check_rules(cold_run) -> None:
    res = copy.deepcopy(cold_run.results)
    assert "trained on synthetic data" in cold.promotion_check(res, None)
    res["data"] |= {"synthetic": False, "source": "travistorrent_5_3_2016"}
    assert "trained on synthetic data" not in cold.promotion_check(res, None)
    res["test"]["final"]["pr_auc"] = 0.2
    res["test"]["final"]["base_rate"] = 0.3
    assert any("base rate" in r for r in cold.promotion_check(res, None))
    res["test"]["final"]["pr_auc"] = 0.9
    res["test"]["final"]["base_rate"] = 0.3
    assert cold.promotion_check(res, None) == []
    assert cold.promotion_check(res, 0.95)  # worse than the current cold champion
    res["model"]["features"] = [*res["model"]["features"], "prev_build_failed"]
    assert any("history" in r for r in cold.promotion_check(res, None))


def test_markdown_uses_the_results(cold_run) -> None:
    text = cold.render_markdown(cold_run.results)
    assert f"{cold_run.results['test']['final']['pr_auc']:.3f}" in text
    assert "release_risk_ci_cold" in text


def test_champion_spec_refuses_baselines(xgb_champion_results) -> None:
    res = copy.deepcopy(xgb_champion_results)
    res["selection"]["final_model"] = "heuristic_rule"
    with pytest.raises(cold.ColdModelError):
        cold.champion_spec(res)


def test_fast_serving_path_equals_the_generic_one(cold_run) -> None:
    """Bundle.assess goes straight to the booster; the numbers must not change."""
    bundle = cold_run.bundle
    rng = np.random.default_rng(1)
    rows = [
        {f: (None if rng.random() < 0.2 else float(rng.integers(0, 300))) for f in bundle.features}
        for _ in range(25)
    ]
    rows[0]["is_pr"] = True
    rows[1]["src_churn"] = "not a number"
    rows[2]["test_churn"] = "12"
    X = bundle.frame(rows)
    generic = bundle.frame(pd.DataFrame(rows))
    np.testing.assert_array_equal(X.to_numpy(), generic.to_numpy())
    out = bundle.assess(rows, full_contributions=True)
    np.testing.assert_allclose([o["raw_score"] for o in out], bundle.model.score(X), rtol=1e-7)
    contrib, base = contributions(bundle.model, X)
    got = np.array([[o["contributions"][f] for f in bundle.features] for o in out])
    np.testing.assert_allclose(got, contrib, rtol=1e-6, atol=1e-7)
    np.testing.assert_allclose([o["base_value"] for o in out], base, rtol=1e-7)
