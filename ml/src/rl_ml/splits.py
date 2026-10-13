"""Evaluation splits (spec section 8.4) and the once-only test-set guard.

* **Chronological (primary):** the mart's global 70/15/15 ``split`` column.
* **Cross-project (secondary):** about 20% of projects are held out entirely, chosen by ranking
  ``sha256(f"{seed}:{project}")`` (deterministic, independent of row order). The model is trained
  on the remaining projects' train rows and evaluated on the held-out projects.
* **Cold start:** test rows with every history feature (``HISTORY_FEATURES`` except
  ``project_age_days``) set to NaN, estimating day-one performance for a new tenant.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

import numpy as np
import pandas as pd

from rl_core.features.schema import HISTORY_FEATURES
from rl_ml.data import Mart

COLD_START_MASKED: Final = tuple(f for f in HISTORY_FEATURES if f != "project_age_days")
T = TypeVar("T")


# ---------------------------------------------------------------- chronological
def chronological_summary(mart: Mart) -> dict[str, Any]:
    """Cut dates and row counts of the mart's chronological split."""
    f = mart.frame
    out: dict[str, Any] = {}
    for name in ("train", "validation", "test"):
        part = f[f["split"] == name]
        out[name] = {
            "rows": len(part),
            "start": part["started_at"].min().isoformat() if len(part) else None,
            "end": part["started_at"].max().isoformat() if len(part) else None,
        }
    tr, va, te = (f[f["split"] == s]["started_at"] for s in ("train", "validation", "test"))
    # A chronological split must not interleave in time.
    out["ordered"] = bool(
        len(tr) and len(va) and len(te) and tr.max() <= va.min() and va.max() <= te.min()
    )
    return out


# ---------------------------------------------------------------- cross-project
def _project_rank_key(project: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{project}".encode()).hexdigest()


def holdout_projects(
    projects: list[str] | np.ndarray, fraction: float = 0.2, seed: int = 0
) -> list[str]:
    """Deterministic ~``fraction`` of distinct projects, by seeded project-name hash."""
    uniq = sorted({str(p) for p in projects})
    if not 0 < fraction < 1:
        raise ValueError("fraction must be in (0, 1)")
    k = max(1, math.floor(len(uniq) * fraction + 0.5))
    ranked = sorted(uniq, key=lambda p: _project_rank_key(p, seed))
    return sorted(ranked[:k])


@dataclass(frozen=True)
class CrossProjectSplit:
    held_out: list[str]
    train: Mart  # seen projects, chronological train rows
    validation: Mart  # seen projects, chronological validation rows (early stopping)
    test_unseen: Mart  # held-out projects, chronological test rows (same window as primary test)
    test_unseen_all: Mart  # held-out projects, every row
    test_seen: Mart  # seen projects, chronological test rows (in-distribution reference)


def cross_project_split(mart: Mart, fraction: float = 0.2, seed: int = 0) -> CrossProjectSplit:
    held = holdout_projects(mart.frame["project"].unique(), fraction, seed)
    is_held = mart.frame["project"].isin(held).to_numpy()
    split = mart.frame["split"].to_numpy()
    return CrossProjectSplit(
        held_out=held,
        train=mart.where(~is_held & (split == "train")),
        validation=mart.where(~is_held & (split == "validation")),
        test_unseen=mart.where(is_held & (split == "test")),
        test_unseen_all=mart.where(is_held),
        test_seen=mart.where(~is_held & (split == "test")),
    )


# ---------------------------------------------------------------- cold start
def cold_start(X: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``X`` with every history feature except ``project_age_days`` set to NaN."""
    out = X.copy()
    for name in COLD_START_MASKED:
        if name in out.columns:
            out[name] = np.nan
    return out


# ---------------------------------------------------------------- once-only guard
class TestSetReuseError(RuntimeError):
    """A held-out evaluation set was opened a second time in the same training run."""

    __test__ = False  # not a pytest test class


@dataclass
class HeldOutSet:
    """Wraps a held-out evaluation set so it can be evaluated exactly once per training run.

    ``evaluate_once(fn, purpose=...)`` calls ``fn(mart)`` and records the access; a second call
    raises :class:`TestSetReuseError`. Model selection must be finished before the call (the
    caller records the locked selection in ``purpose``).
    """

    name: str
    _mart: Mart = field(repr=False)
    access_log: list[dict[str, str]] = field(default_factory=list)

    @property
    def opened(self) -> bool:
        return bool(self.access_log)

    @property
    def rows(self) -> int:
        return len(self._mart.frame)

    def evaluate_once(self, fn: Callable[[Mart], T], *, purpose: str) -> T:
        if self.access_log:
            raise TestSetReuseError(
                f"held-out set {self.name!r} was already evaluated "
                f"({self.access_log[0]['purpose']!r} at {self.access_log[0]['at']}); "
                "a training run may evaluate it only once"
            )
        self.access_log.append({"purpose": purpose, "at": datetime.now(UTC).isoformat()})
        return fn(self._mart)
