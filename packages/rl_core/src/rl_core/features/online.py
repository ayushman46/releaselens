"""Per-request feature computation (online / serving).

Combines the change facts from the GitHub Action payload with the tenant's stored build history
(``ci_runs``: only runs that had *finished* before the request time count) through the exact same
functions the offline batch uses.

Bounded history. A serving request should not load a repository's entire history. Every history
feature except two only looks at the last :data:`~rl_core.features.history.ZSCORE_WINDOW` (20)
eligible builds, the previous start time, and the most recent failure:

* ``history_size`` counts *all* eligible builds, and
* ``builds_since_last_failure`` counts back to the most recent failure, which may be older than
  the window.

So the API loads the most recent eligible runs (plus the latest start before the request) and
passes the two exact totals as :class:`HistoryTotals`; the result is then identical to computing
over the full history (``ml/tests/test_serving_parity.py`` checks this on the real mart).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from rl_core.features.change import BUILD_INPUT_FIELDS, change_features, time_features
from rl_core.features.history import ZSCORE_WINDOW, History, history_features
from rl_core.features.schema import ALL_FEATURE_NAMES


@dataclass(frozen=True, slots=True)
class PastRun:
    started_at: datetime
    finished_at: datetime | None  # None = still running / unknown → never counts
    failed: bool | None  # None = no outcome (canceled)
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class HistoryTotals:
    """Exact totals over the *whole* eligible history, for a request given only its tail.

    ``eligible`` = number of past builds with an outcome that finished before the request.
    ``since_last_failure`` = eligible builds after the most recent eligible failure (ordered by
    finish time), or ``None`` if no eligible build ever failed.
    """

    eligible: int
    since_last_failure: int | None

    def __post_init__(self) -> None:
        if self.eligible < 0:
            raise ValueError("eligible must be >= 0")
        if self.since_last_failure is not None and not (
            0 <= self.since_last_failure < max(self.eligible, 1)
        ):
            raise ValueError("since_last_failure must be within 0..eligible-1")


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
    *,
    totals: HistoryTotals | None = None,
) -> dict[str, float | None]:
    """Feature vector (``ALL_FEATURE_NAMES`` order) for one build/check triggered at started_at.

    ``history`` may be the full history, or (with ``totals``) only its most recent eligible
    builds plus the latest build that started before ``started_at``; see the module docstring.
    """
    unknown = set(change_facts) - set(BUILD_INPUT_FIELDS)
    if unknown:
        raise ValueError(f"unknown change facts: {sorted(unknown)}")
    t = _ts(started_at)
    inputs = {k: float("nan") if v is None else float(v) for k, v in change_facts.items()}
    feats: dict[str, np.ndarray] = {}
    feats.update(change_features(inputs, 1))
    feats.update(history_features(history_from_runs(history), [t]))
    feats.update(time_features([t]))
    if totals is not None:
        feats["history_size"] = np.array([float(totals.eligible)])
        feats["builds_since_last_failure"] = np.array(
            [
                float("nan")
                if totals.eligible == 0 or totals.since_last_failure is None
                else float(totals.since_last_failure)
            ]
        )
    out: dict[str, float | None] = {}
    for name in ALL_FEATURE_NAMES:
        v = float(feats[name][0])
        out[name] = None if np.isnan(v) else v
    return out


def _eligible(runs: Sequence[PastRun], t: float) -> list[tuple[float, int, PastRun]]:
    """Eligible runs (outcome known, finished strictly before t), ordered by finish time with
    ties in input order: the order :func:`history_features` uses."""
    out = [
        (_ts(r.finished_at), i, r)
        for i, r in enumerate(runs)
        if r.failed is not None and r.finished_at is not None and _ts(r.finished_at) < t
    ]
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def history_totals(runs: Sequence[PastRun], started_at: datetime) -> HistoryTotals:
    """Exact :class:`HistoryTotals` from a full history."""
    eligible = _eligible(runs, _ts(started_at))
    last_fail = max((pos for pos, e in enumerate(eligible) if e[2].failed), default=None)
    since = None if last_fail is None else len(eligible) - 1 - last_fail
    return HistoryTotals(eligible=len(eligible), since_last_failure=since)


def bounded_history(
    runs: Sequence[PastRun], started_at: datetime, limit: int
) -> tuple[list[PastRun], HistoryTotals]:
    """What the serving path loads: the ``limit`` most recent eligible runs (finish order) plus
    the latest run that started before ``started_at``, and the exact totals.

    ``compute_online(facts, window, t, totals=totals)`` equals ``compute_online(facts, runs, t)``
    whenever ``limit`` >= the history window (20). The API's SQL implements the same selection
    (``rl_db.repositories.risk.load_history``); tests hold the two together.
    """
    if limit < ZSCORE_WINDOW:  # fail_rate_last_20 and the duration z-score look back 20 builds
        raise ValueError(f"limit must be >= {ZSCORE_WINDOW} (the longest history window)")
    t = _ts(started_at)
    window = [e[2] for e in _eligible(runs, t)[-limit:]]
    starts = [(r.started_at, i) for i, r in enumerate(runs) if _ts(r.started_at) < t]
    if starts:
        latest = max(starts, key=lambda s: (_ts(s[0]), s[1]))[0]
        window.append(PastRun(started_at=latest, finished_at=None, failed=None, duration_s=None))
    return window, history_totals(runs, started_at)
