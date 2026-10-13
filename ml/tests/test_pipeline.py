"""End-to-end training pipeline on SYNTHETIC data: guards, outputs, bundle, reproducibility."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import polars as pl
import pytest

from rl_core.features.schema import ALL_FEATURE_NAMES, HISTORY_FEATURES
from rl_ml import card
from rl_ml.bands import BANDS
from rl_ml.data import MartSchemaError, attach_status, class_balance, exclude_errored, prepare_mart
from rl_ml.predictor import RiskModelBundle
from rl_ml.splits import (
    COLD_START_MASKED,
    HeldOutSet,
    TestSetReuseError,
    chronological_summary,
    cold_start,
    cross_project_split,
    holdout_projects,
)
from rl_ml.train import run_training


# ---------------------------------------------------------------- data contract
def test_prepare_mart_rejects_missing_columns(synthetic_mart) -> None:
    df = synthetic_mart.frame.drop(columns=["prev_build_failed"])
    with pytest.raises(MartSchemaError, match="prev_build_failed"):
        prepare_mart(df, run_id="x", source="synthetic", dataset_md5=None)


def test_prepare_mart_rejects_wrong_schema_version_and_bad_labels(synthetic_mart) -> None:
    with pytest.raises(MartSchemaError):
        prepare_mart(
            synthetic_mart.frame, run_id="x", source="s", dataset_md5=None, schema_version="0.9.0"
        )
    df = synthetic_mart.frame.copy()
    df.loc[0, "label"] = 2
    with pytest.raises(MartSchemaError):
        prepare_mart(df, run_id="x", source="s", dataset_md5=None)
    df = synthetic_mart.frame.assign(feature_schema_version="2.0.0")
    with pytest.raises(MartSchemaError):
        prepare_mart(df, run_id="x", source="s", dataset_md5=None)


def test_prepare_mart_accepts_polars_and_sorts_chronologically(synthetic_mart) -> None:
    pl_df = pl.from_pandas(synthetic_mart.frame.sample(frac=1.0, random_state=0))
    m = prepare_mart(pl_df, run_id="x", source="synthetic", dataset_md5=None)
    assert m.frame["started_at"].is_monotonic_increasing
    assert len(m.frame) == len(synthetic_mart.frame)


def test_errored_exclusion_drops_only_errored_rows(synthetic_mart) -> None:
    m = exclude_errored(synthetic_mart)
    assert "errored" not in set(m.frame["status"])
    n_err = int((synthetic_mart.frame["status"] == "errored").sum())
    assert len(m.frame) == len(synthetic_mart.frame) - n_err
    assert m.notes[-1] == f"errored builds excluded: {n_err}"


def test_attach_status_joins_raw_status_and_checks_labels(synthetic_mart) -> None:
    status = synthetic_mart.frame[["build_id", "status"]]
    no_status = prepare_mart(
        synthetic_mart.frame.drop(columns=["status"]), run_id="x", source="s", dataset_md5=None
    )
    joined = attach_status(no_status, status)
    assert (
        joined.frame["status"].isin(["failed", "errored"]).astype(int) == joined.frame["label"]
    ).all()
    wrong = status.assign(status="passed")
    with pytest.raises(MartSchemaError):
        attach_status(no_status, wrong)


def test_class_balance_reports_every_split(synthetic_mart) -> None:
    cb = class_balance(synthetic_mart)
    assert sum(cb[s]["rows"] for s in ("train", "validation", "test")) == cb["all"]["rows"]
    assert 0 < cb["all"]["base_rate"] < 1


# ---------------------------------------------------------------- splits
def test_chronological_split_is_ordered(synthetic_mart) -> None:
    s = chronological_summary(synthetic_mart)
    assert s["ordered"]
    assert (
        s["train"]["end"]
        <= s["validation"]["start"]
        <= s["validation"]["end"]
        <= s["test"]["start"]
    )


def test_holdout_projects_are_deterministic_and_about_20_percent() -> None:
    projects = [f"org/p{i}" for i in range(50)]
    a = holdout_projects(projects, 0.2, seed=1)
    assert len(a) == 10
    assert a == holdout_projects(list(reversed(projects)), 0.2, seed=1)
    assert a != holdout_projects(projects, 0.2, seed=2)


def test_cross_project_split_never_trains_on_held_out_projects(synthetic_mart) -> None:
    cp = cross_project_split(synthetic_mart, 0.2, seed=3)
    held = set(cp.held_out)
    assert not held & set(cp.train.frame["project"])
    assert not held & set(cp.validation.frame["project"])
    assert set(cp.test_unseen_all.frame["project"]) <= held
    assert set(cp.test_unseen.frame["split"]) <= {"test"}


def test_cold_start_masks_history_except_project_age(synthetic_mart) -> None:
    X = synthetic_mart.X(ALL_FEATURE_NAMES)
    masked = cold_start(X)
    assert set(COLD_START_MASKED) == set(HISTORY_FEATURES) - {"project_age_days"}
    for f in COLD_START_MASKED:
        assert masked[f].isna().all()
    pd.testing.assert_series_equal(masked["project_age_days"], X["project_age_days"])
    pd.testing.assert_series_equal(masked["src_churn"], X["src_churn"])


def test_held_out_set_can_be_evaluated_only_once(synthetic_mart) -> None:
    guard = HeldOutSet("test", synthetic_mart.split("test"))
    assert guard.evaluate_once(lambda m: len(m.frame), purpose="first") == guard.rows
    with pytest.raises(TestSetReuseError):
        guard.evaluate_once(lambda m: len(m.frame), purpose="second")
    assert len(guard.access_log) == 1


# ---------------------------------------------------------------- the training run
def test_run_evaluates_the_test_set_exactly_once(trained_run) -> None:
    r = trained_run.results
    assert r["test"]["evaluations"] == 1
    assert all(len(g.access_log) == 1 for g in trained_run.guards)
    with pytest.raises(TestSetReuseError):
        trained_run.guards[0].evaluate_once(lambda m: None, purpose="peek")


def test_final_model_is_the_best_on_validation(trained_run) -> None:
    r = trained_run.results
    eligible = [c for c in r["candidates"] if c["name"] in r["selection"]["eligible"]]
    assert r["selection"]["val_pr_auc"] == max(c["val_pr_auc"] for c in eligible)
    tf = r["time_features"]
    assert tf["time_features_used"] == (tf["val_pr_auc_with"] > tf["val_pr_auc_without"])
    names = {c["name"] for c in r["candidates"]}
    assert {
        "majority_class",
        "heuristic_rule",
        "logistic_regression",
        "random_forest",
        "xgboost",
        "xgboost_time",
    } <= names


def test_results_are_strict_json_and_marked_synthetic(trained_run) -> None:
    text = json.dumps(trained_run.results, allow_nan=False)
    r = json.loads(text)
    assert r["data"]["synthetic"] is True
    assert r["test"]["calibration"]["gate"]["thresholds"]["ece_max"] == 0.05
    assert set(r["bands"]["raw"]) == {"MEDIUM", "HIGH", "CRITICAL"}


def test_majority_baseline_pr_auc_is_the_base_rate(trained_run) -> None:
    ref = trained_run.results["test"]["reference_candidates"]["majority_class"]
    assert ref["pr_auc"] == pytest.approx(ref["base_rate"])


def test_bundle_round_trip_and_assessment(trained_run, synthetic_mart, tmp_path) -> None:
    trained_run.bundle.save(tmp_path / "bundle")
    loaded = RiskModelBundle.load(tmp_path / "bundle")
    X = synthetic_mart.split("validation").X(trained_run.final.features).head(25)
    a = trained_run.bundle.assess(X)
    b = loaded.assess(X)
    for x, y in zip(a, b, strict=True):
        assert x["risk_score"] == y["risk_score"]
        assert x["risk_band"] == y["risk_band"]
        assert x["raw_score"] == pytest.approx(y["raw_score"], rel=1e-6)
        assert 0 <= x["risk_score"] <= 100
        assert x["risk_band"] in BANDS
        assert (x["probability"] is None) == (
            not trained_run.results["test"]["probability_exposed"]
        )
        assert len(x["top_increasing"]) <= 3
        assert len(x["top_reducing"]) <= 3
    meta = json.loads((tmp_path / "bundle" / "metadata.json").read_text())
    assert meta["bands"]["raw"] == trained_run.results["bands"]["raw"]
    assert len(meta["score_scale"]["validation_quantiles"]) == 1001
    if not meta["probability_exposed"]:
        assert meta["calibrator"] is None


def test_bundle_treats_missing_inputs_as_unknown(trained_run) -> None:
    out = trained_run.bundle.assess([{"src_churn": 12.0}])[0]
    assert 0 <= out["risk_score"] <= 100


def test_same_seed_reproduces_metrics(synthetic_mart, fast_config, trained_run) -> None:
    again = run_training(synthetic_mart, fast_config).results
    first = trained_run.results
    for key in ("pr_auc", "roc_auc", "lift_at_top_10pct"):
        assert again["test"]["final"][key] == pytest.approx(first["test"]["final"][key], abs=1e-9)
    assert again["selection"]["final_model"] == first["selection"]["final_model"]
    assert again["bands"]["raw"] == pytest.approx(first["bands"]["raw"], abs=1e-9)
    for a, b in zip(again["candidates"], first["candidates"], strict=True):
        assert a["val_pr_auc"] == pytest.approx(b["val_pr_auc"], abs=1e-9)


def test_errored_excluded_variant_runs(synthetic_mart, fast_config) -> None:
    from dataclasses import replace

    cfg = replace(
        fast_config,
        errored_excluded=True,
        families=("majority_class", "heuristic_rule", "logistic_regression"),
    )
    r = run_training(synthetic_mart, cfg).results
    assert r["variant"] == "errored_excluded"
    assert "errored" not in r["class_balance"]["all"]["status_counts"]


# ---------------------------------------------------------------- docs generation
def test_card_refuses_synthetic_results(trained_run, tmp_path) -> None:
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "failed_or_errored.json").write_text(json.dumps(trained_run.results))
    with pytest.raises(card.SyntheticResultsError):
        card.write_docs(tmp_path)


def test_card_without_results_says_tbd(tmp_path) -> None:
    a, b = card.write_docs(tmp_path)
    assert card.TBD in a.read_text()
    assert card.TBD in b.read_text()


def test_rendering_uses_only_numbers_from_json(trained_run) -> None:
    r = trained_run.results
    text = card.render_results({"failed_or_errored": r})
    assert card.f(r["test"]["final"]["pr_auc"]) in text
    assert card.f(r["test"]["final"]["base_rate"]) in text
    model_card = card.render_model_card(
        {
            "failed_or_errored": r
            | {
                "registry": {
                    "run_id": "abc",
                    "version": "4",
                    "registered_model": "release_risk_ci",
                    "alias": "challenger",
                }
            }
        }
    )
    assert "release_risk_ci/4" in model_card
    assert "CI failure ≠ outage" in model_card


def test_plots_are_small(trained_run, tmp_path) -> None:
    from rl_ml.train import write_outputs

    paths = write_outputs(trained_run, tmp_path)
    for key in ("pr_curve", "reliability"):
        assert paths[key].stat().st_size < 100_000
    assert np.isfinite(json.loads(paths["results"].read_text())["test"]["final"]["pr_auc"])
