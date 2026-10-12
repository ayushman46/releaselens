"""Batch feature computation over a Polars frame of build inputs (offline / training).

Input columns (one row per build, see ``REQUIRED_COLUMNS`` + ``BUILD_INPUT_FIELDS``):
``build_id, project, started_at, finished_at, failed, duration_s`` plus change inputs.
``finished_at`` may be the upper bound ``started_at + duration`` (ADR-0006, DATASET_AUDIT §4.1).
``failed`` is 1/0, or null for builds without an outcome (canceled) — those never count as
history. The label of a row is never used for that row's own features (strict ``<``).
"""

from __future__ import annotations

from typing import Final

import numpy as np
import polars as pl

from rl_core.features.change import BUILD_INPUT_FIELDS, change_features, time_features
from rl_core.features.history import History, history_features, max_input_finished_at
from rl_core.features.schema import ALL_FEATURE_NAMES

REQUIRED_COLUMNS: Final = (
    "build_id",
    "project",
    "started_at",
    "finished_at",
    "failed",
    "duration_s",
)
AUDIT_COLUMN: Final = "max_history_input_finished_at"


def _epoch_s(series: pl.Series) -> np.ndarray:
    """Datetime (any unit, tz-aware or naive-UTC) → float epoch seconds, NaN for nulls."""
    us = series.dt.cast_time_unit("us").dt.epoch("us").cast(pl.Float64)
    return us.fill_null(np.nan).to_numpy() / 1e6


def compute_features(builds: pl.DataFrame) -> pl.DataFrame:
    """Return ``build_id`` + every feature in ``ALL_FEATURE_NAMES`` + the audit column."""
    missing = [c for c in REQUIRED_COLUMNS if c not in builds.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")
    if builds["build_id"].n_unique() != builds.height:
        raise ValueError("build_id must be unique (one row per build)")
    if builds["started_at"].null_count():
        raise ValueError("started_at must not be null (the prediction moment)")

    parts: list[pl.DataFrame] = []
    for (_project,), grp in builds.sort("build_id").group_by(["project"], maintain_order=True):
        started = _epoch_s(grp["started_at"])
        hist = History(
            finished_at=_epoch_s(grp["finished_at"]),
            failed=grp["failed"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
            duration=grp["duration_s"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
            started_at=started,
        )
        feats: dict[str, np.ndarray] = {}
        inputs: dict[str, np.ndarray] = {
            name: grp[name].cast(pl.Float64).fill_null(np.nan).to_numpy()
            for name in BUILD_INPUT_FIELDS
            if name in grp.columns
        }
        feats.update(change_features(inputs, grp.height))
        feats.update(history_features(hist, started))
        feats.update(time_features(started))
        audit = max_input_finished_at(hist, started)
        frame = pl.DataFrame(
            {"build_id": grp["build_id"]}
            | {name: feats[name] for name in ALL_FEATURE_NAMES}
            | {AUDIT_COLUMN: audit}
        )
        parts.append(frame)

    if not parts:
        schema = {"build_id": pl.Int64} | dict.fromkeys(ALL_FEATURE_NAMES, pl.Float64)
        return pl.DataFrame(schema=schema | {AUDIT_COLUMN: pl.Float64})
    out = pl.concat(parts).sort("build_id")
    # NaN → null so downstream (Parquet, XGBoost via Arrow) sees proper missing values.
    return out.with_columns(pl.col(pl.Float64).fill_nan(None))
