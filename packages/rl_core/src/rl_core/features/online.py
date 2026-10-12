"""Per-request feature computation (online / serving).

Combines the change facts from the GitHub Action payload with the tenant's stored build history
(``ci_runs``: only runs that had *finished* before the request time count) through the exact same
functions the offline batch uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from rl_core.features.change import BUILD_INPUT_FIELDS, change_features, time_features
from rl_core.features.history import History, history_features
from rl_core.features.schema import ALL_FEATURE_NAMES


@dataclass(frozen=True, slots=True)
class PastRun:
    started_at: datetime
    finished_at: datetime | None  # None = still running / unknown → never counts
    failed: bool | None  # None = no outcome (canceled)
    duration_s: float | None


def _ts(dt: datetime | None) -> float:
    if dt is None:
        return float("nan")
    if dt.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware (UTC)")
    return dt.timestamp()


def history_from_runs(runs: Sequence[PastRun]) -> History:
    if not runs:
        return History.empty()
    return History(
        finished_at=np.array([_ts(r.finished_at) for r in runs]),
        failed=np.array([float("nan") if r.failed is None else float(r.failed) for r in runs]),
        duration=np.array([float("nan") if r.duration_s is None else r.duration_s for r in runs]),
        started_at=np.array([_ts(r.started_at) for r in runs]),
    )


def compute_online(
    change_facts: Mapping[str, float | int | bool | None],
    history: Sequence[PastRun],
    started_at: datetime,
) -> dict[str, float | None]:
    """Feature vector (``ALL_FEATURE_NAMES`` order) for one build/check triggered at started_at."""
    unknown = set(change_facts) - set(BUILD_INPUT_FIELDS)
    if unknown:
        raise ValueError(f"unknown change facts: {sorted(unknown)}")
    t = _ts(started_at)
    inputs = {k: float("nan") if v is None else float(v) for k, v in change_facts.items()}
    feats: dict[str, np.ndarray] = {}
    feats.update(change_features(inputs, 1))
    feats.update(history_features(history_from_runs(history), [t]))
    feats.update(time_features([t]))
    out: dict[str, float | None] = {}
    for name in ALL_FEATURE_NAMES:
        v = float(feats[name][0])
        out[name] = None if np.isnan(v) else v
    return out
