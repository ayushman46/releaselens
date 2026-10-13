"""Shared fixtures. All model fixtures use SYNTHETIC data (rl_ml.synthetic) — never real metrics."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")


@pytest.fixture(scope="session")
def synthetic_mart():
    from rl_ml.data import prepare_mart
    from rl_ml.synthetic import SYNTHETIC_SOURCE, make_synthetic_mart

    df = make_synthetic_mart(n_projects=24, builds_per_project=(60, 200), seed=7)
    return prepare_mart(df, run_id="synthetic-test", source=SYNTHETIC_SOURCE, dataset_md5=None)


@pytest.fixture(scope="session")
def fast_config():
    from rl_ml.train import TrainConfig

    return TrainConfig(
        trials=3, seed=11, bootstrap_resamples=40, shap_sample=500, min_project_builds=20
    )


@pytest.fixture(scope="session")
def trained_run(synthetic_mart, fast_config):
    from rl_ml.train import run_training

    return run_training(synthetic_mart, fast_config)
