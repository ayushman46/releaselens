"""SYNTHETIC feature-mart generator — for tests and smoke runs ONLY.

Nothing produced from this data is a real result. Frames carry ``source="synthetic"`` and the
report generator (:mod:`rl_ml.card`) refuses to write ``docs/ml`` from a synthetic run.

The generator produces *build inputs* (change facts, a timeline and outcomes) and runs them
through the real :func:`rl_core.features.offline.compute_features`, so the history features
follow the same finished-before rule as the real mart. Outcomes depend on change size, a
per-project propensity and the previous build's outcome, which gives the models signal to find.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl

from rl_core.features.offline import compute_features
from rl_core.features.schema import ALL_FEATURE_NAMES

SYNTHETIC_SOURCE = "synthetic"


def _project_builds(
    rng: np.random.Generator, project: str, n: int, t0: float, span: float
) -> dict[str, Any]:
    """Inputs + outcomes for one synthetic project, in start-time order."""
    start = t0 + rng.uniform(0, span * 0.3)
    gaps = rng.exponential(span * 0.7 / max(n, 1), size=n)
    started = start + np.cumsum(gaps)
    duration = rng.lognormal(6.3, 0.6, size=n)  # ~9 minutes
    propensity = rng.normal(-1.3, 0.6)

    src = np.where(rng.random(n) < 0.3, 0, np.round(rng.lognormal(3.0, 1.6, size=n)))
    test = np.where(rng.random(n) < 0.7, 0, np.round(rng.lognormal(2.5, 1.4, size=n)))
    files_mod = rng.poisson(1 + np.log1p(src) / 2)
    team = max(1, int(rng.integers(2, 30)))
    sloc = float(rng.lognormal(10.5, 1.0))
    is_pr = (rng.random(n) < rng.uniform(0.1, 0.5)).astype(np.float64)

    failed = np.zeros(n)
    prev = 0.0
    for i in range(n):
        logit = (
            propensity
            + 0.25 * (np.log1p(src[i]) - 3.0)
            + 1.4 * prev
            + 0.3 * is_pr[i]
            - 0.15 * (test[i] > 0)
        )
        failed[i] = float(rng.random() < 1.0 / (1.0 + np.exp(-logit)))
        prev = failed[i]

    return {
        "project": [project] * n,
        "started_at": started,
        "duration_s": duration,
        "failed": failed,
        "src_churn": src,
        "test_churn": test,
        "files_added": rng.poisson(0.4, size=n).astype(np.float64),
        "files_deleted": rng.poisson(0.1, size=n).astype(np.float64),
        "files_modified": files_mod.astype(np.float64),
        "tests_added": rng.poisson(0.2, size=n).astype(np.float64),
        "tests_deleted": rng.poisson(0.05, size=n).astype(np.float64),
        "src_files": rng.poisson(1.5, size=n).astype(np.float64),
        "doc_files": rng.poisson(0.1, size=n).astype(np.float64),
        "other_files": rng.poisson(0.5, size=n).astype(np.float64),
        "sloc": np.full(n, sloc),
        "test_lines_per_kloc": np.full(n, rng.uniform(50, 800)),
        "test_cases_per_kloc": np.full(n, rng.uniform(1, 40)),
        "asserts_per_kloc": np.full(n, rng.uniform(5, 120)),
        "num_all_built_commits": (rng.geometric(0.5, size=n)).astype(np.float64),
        "team_size": np.full(n, float(team)),
        "by_core_team_member": (rng.random(n) < 0.8).astype(np.float64),
        "is_pr": is_pr,
        "commits_on_files_touched": rng.poisson(5, size=n).astype(np.float64),
        "repo_age_days": (started - start) / 86_400.0 + rng.uniform(0, 400),
    }


def make_synthetic_mart(
    n_projects: int = 30,
    builds_per_project: tuple[int, int] = (80, 300),
    seed: int = 0,
    start: datetime = datetime(2012, 1, 1, tzinfo=UTC),
    span_days: float = 900.0,
) -> pl.DataFrame:
    """A SYNTHETIC frame with the real mart's columns (+ ``status``) and a 70/15/15 time split."""
    rng = np.random.default_rng(seed)
    t0 = start.timestamp()
    span = span_days * 86_400.0
    cols: dict[str, list[Any]] = {}
    for p in range(n_projects):
        n = int(rng.integers(builds_per_project[0], builds_per_project[1] + 1))
        part = _project_builds(rng, f"synthetic/project-{p:03d}", n, t0, span)
        for k, v in part.items():
            cols.setdefault(k, []).extend(list(v))

    n_total = len(cols["project"])
    started_us = (np.asarray(cols["started_at"]) * 1e6).astype(np.int64)
    finished_us = started_us + (np.asarray(cols["duration_s"]) * 1e6).astype(np.int64)
    failed = np.asarray(cols["failed"])
    # A share of failures are "errored" so the errored-excluded variant has something to drop.
    errored = (failed > 0.5) & (rng.random(n_total) < 0.4)
    status = np.where(failed > 0.5, np.where(errored, "errored", "failed"), "passed")

    inputs = pl.DataFrame(
        {
            "build_id": np.arange(1, n_total + 1, dtype=np.int64),
            "project": cols["project"],
            "started_at": pl.Series(started_us).cast(pl.Datetime("us", "UTC")),
            "finished_at": pl.Series(finished_us).cast(pl.Datetime("us", "UTC")),
            "failed": failed.astype(np.int64),
            "duration_s": np.asarray(cols["duration_s"]),
        }
        | {
            k: np.asarray(v, dtype=np.float64)
            for k, v in cols.items()
            if k not in {"project", "started_at", "duration_s", "failed"}
        }
    )
    inputs = inputs.with_columns(status=pl.Series(status))
    feats = compute_features(inputs)
    frame = (
        inputs.select(
            "build_id", "project", "started_at", pl.col("failed").alias("label"), "status"
        )
        .join(feats, on="build_id", how="inner")
        .sort("started_at", "build_id")
    )
    n = frame.height
    split = np.full(n, "test", dtype=object)
    split[: int(n * 0.70)] = "train"
    split[int(n * 0.70) : int(n * 0.85)] = "validation"
    frame = frame.with_columns(split=pl.Series(split.astype(str)))
    ordered = ["build_id", "project", "started_at", "label", "status", "split"]
    return frame.select([*ordered, *ALL_FEATURE_NAMES, "max_history_input_finished_at"]).sort(
        "build_id"
    )
