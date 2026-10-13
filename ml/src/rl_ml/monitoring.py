"""Feature drift monitoring helpers (spec section 8.10): Population Stability Index.

Training bins are equal-mass deciles of each feature's non-null training values (duplicate
edges collapse, so heavily tied features get fewer bins) plus a separate *missing* bucket. They
are stored as a JSON artifact with the model; the API / Model page calls
:func:`compute_psi` with live feature values.

PSI = sum_i (a_i - e_i) * ln(a_i / e_i), with proportions floored at 1e-4. Conventional reading:
< 0.10 stable, 0.10-0.25 moderate shift, > 0.25 major shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pandas as pd

PSI_FLOOR: Final = 1e-4
PSI_MODERATE: Final = 0.10
PSI_MAJOR: Final = 0.25


@dataclass(frozen=True)
class FeatureBins:
    edges: tuple[float, ...]  # interior cut points; bin i = (edges[i-1], edges[i]]
    expected: tuple[float, ...]  # proportions: len(edges)+1 value bins, then the missing bucket

    def to_dict(self) -> dict[str, Any]:
        return {"edges": list(self.edges), "expected": list(self.expected)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> FeatureBins:
        return FeatureBins(
            tuple(float(x) for x in d["edges"]), tuple(float(x) for x in d["expected"])
        )


def _counts(edges: np.ndarray, values: np.ndarray) -> np.ndarray:
    v = np.asarray(values, dtype=np.float64)
    miss = np.isnan(v)
    idx = np.searchsorted(edges, v[~miss], side="left")
    counts = np.bincount(idx, minlength=len(edges) + 1).astype(np.float64)
    return np.append(counts, float(miss.sum()))


def fit_bins(values: npt.ArrayLike, n_bins: int = 10) -> FeatureBins:
    v = np.asarray(values, dtype=np.float64)
    ok = v[~np.isnan(v)]
    edges = (
        np.unique(np.quantile(ok, np.linspace(0, 1, n_bins + 1)[1:-1])) if ok.size else np.array([])
    )
    counts = _counts(edges, v)
    return FeatureBins(
        tuple(float(e) for e in edges), tuple(float(c) for c in counts / max(len(v), 1))
    )


def training_bins(X_train: pd.DataFrame, n_bins: int = 10) -> dict[str, FeatureBins]:
    return {
        str(c): fit_bins(X_train[c].to_numpy(dtype=np.float64), n_bins) for c in X_train.columns
    }


def compute_psi(
    train_bins: FeatureBins | dict[str, Any], live_values: npt.ArrayLike
) -> dict[str, Any]:
    """PSI of live values against one feature's training bins."""
    bins = train_bins if isinstance(train_bins, FeatureBins) else FeatureBins.from_dict(train_bins)
    edges = np.asarray(bins.edges, dtype=np.float64)
    live = np.asarray(live_values, dtype=np.float64).reshape(-1)
    if live.size == 0:
        raise ValueError("no live values")
    actual = _counts(edges, live) / live.size
    expected = np.asarray(bins.expected, dtype=np.float64)
    a = np.maximum(actual, PSI_FLOOR)
    e = np.maximum(expected, PSI_FLOOR)
    terms = (a - e) * np.log(a / e)
    psi = float(terms.sum())
    status = "major" if psi > PSI_MAJOR else "moderate" if psi >= PSI_MODERATE else "stable"
    return {
        "psi": psi,
        "status": status,
        "n_live": int(live.size),
        "expected": expected.tolist(),
        "actual": actual.tolist(),
        "terms": terms.tolist(),
    }


def psi_report(bins: dict[str, FeatureBins], X_live: pd.DataFrame) -> dict[str, dict[str, Any]]:
    return {
        name: {
            k: v
            for k, v in compute_psi(b, X_live[name].to_numpy()).items()
            if k in {"psi", "status", "n_live"}
        }
        for name, b in bins.items()
        if name in X_live.columns
    }
