"""The SYNTHETIC fixture is internally consistent with rl_core and with dbt's naive link."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from rl_analysis.testing import SYNTHETIC_BUILDS, SYNTHETIC_NAIVE
from rl_core.features.history import History, history_features

_LABEL = {"passed": 0.0, "failed": 1.0, "errored": 1.0}


def _epoch(text: str) -> float:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC).timestamp()


def test_hand_derived_prev_build_failed_matches_rl_core() -> None:
    for project in sorted({b[1] for b in SYNTHETIC_BUILDS}):
        rows = [b for b in SYNTHETIC_BUILDS if b[1] == project]
        starts = np.asarray([_epoch(b[2]) for b in rows])
        history = History(
            finished_at=starts + np.asarray([b[3] for b in rows], dtype=np.float64),
            failed=np.asarray([_LABEL.get(b[4], math.nan) for b in rows]),
            duration=np.asarray([b[3] for b in rows], dtype=np.float64),
            started_at=starts,
        )
        computed = history_features(history, starts)["prev_build_failed"]
        for row, value in zip(rows, computed, strict=True):
            if row[4] not in _LABEL:
                continue  # canceled builds are not in the feature mart
            expected = row[9]
            if expected is None:
                assert math.isnan(value), row[0]
            else:
                assert value == expected, row[0]


def test_hand_derived_naive_links_follow_start_order() -> None:
    for project in sorted({b[1] for b in SYNTHETIC_BUILDS}):
        rows = sorted((b for b in SYNTHETIC_BUILDS if b[1] == project), key=lambda b: (b[2], b[0]))
        previous = None
        for row in rows:
            link = SYNTHETIC_NAIVE[row[0]]
            if previous is None:
                assert link == (None, None, None)
            else:
                finished = _epoch(previous[2]) + previous[3] < _epoch(row[2])
                label = {"passed": 0, "failed": 1, "errored": 1}.get(previous[4])
                assert link == (previous[0], label, finished), row[0]
            previous = row
