"""User-facing risk score and risk bands (ADR-0012, spec section 8.7).

* ``risk_score`` = percentile (0-100) of the raw model score among **validation** predictions,
  served from a stored quantile table (1,001 validation quantiles) in the model metadata.
* Bands are cut on validation by recall targets: MEDIUM = the highest raw-score threshold that
  still reaches recall 0.80, HIGH = recall 0.50, CRITICAL = recall 0.20. The cut-points live in
  the model metadata and are never hard-coded in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt

BANDS: Final = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
RECALL_TARGETS: Final = {"MEDIUM": 0.80, "HIGH": 0.50, "CRITICAL": 0.20}
N_QUANTILES: Final = 1001


@dataclass(frozen=True)
class ScoreScale:
    """Validation score quantiles → percentile risk score."""

    quantiles: tuple[float, ...]

    @staticmethod
    def fit(val_scores: npt.ArrayLike, n: int = N_QUANTILES) -> ScoreScale:
        s = np.asarray(val_scores, dtype=np.float64)
        q = np.quantile(s, np.linspace(0.0, 1.0, n), method="linear")
        return ScoreScale(tuple(float(v) for v in q))

    def percentile(self, raw: npt.ArrayLike) -> np.ndarray:
        """Share of validation scores <= raw, in percent (resolution 0.1)."""
        q = np.asarray(self.quantiles)
        idx = np.searchsorted(q, np.asarray(raw, dtype=np.float64), side="right")
        return np.clip(100.0 * (idx - 1) / (len(q) - 1), 0.0, 100.0)

    def risk_score(self, raw: npt.ArrayLike) -> np.ndarray:
        return np.rint(self.percentile(raw)).astype(np.int64)


def threshold_for_recall(y: npt.ArrayLike, s: npt.ArrayLike, target: float) -> float:
    """Highest threshold t with recall(score >= t) >= target."""
    ya = np.asarray(y, dtype=np.int64)
    sa = np.asarray(s, dtype=np.float64)
    pos = int(ya.sum())
    if pos == 0:
        raise ValueError("no positives: recall is undefined")
    order = np.argsort(-sa, kind="stable")
    tp = np.cumsum(ya[order])
    k = int(np.searchsorted(tp, np.ceil(target * pos - 1e-9), side="left"))
    return float(sa[order][min(k, len(sa) - 1)])


@dataclass(frozen=True)
class BandCuts:
    """Raw-score cut-points: band = highest level whose cut the score reaches."""

    medium: float
    high: float
    critical: float

    @staticmethod
    def fit(y_val: npt.ArrayLike, val_scores: npt.ArrayLike) -> BandCuts:
        cuts = {b: threshold_for_recall(y_val, val_scores, r) for b, r in RECALL_TARGETS.items()}
        # monotone by construction (lower recall target → higher threshold); enforce anyway
        med = cuts["MEDIUM"]
        high = max(cuts["HIGH"], med)
        crit = max(cuts["CRITICAL"], high)
        return BandCuts(med, high, crit)

    def band(self, raw: npt.ArrayLike) -> np.ndarray:
        s = np.asarray(raw, dtype=np.float64)
        out = np.full(s.shape, "LOW", dtype=object)
        out[s >= self.medium] = "MEDIUM"
        out[s >= self.high] = "HIGH"
        out[s >= self.critical] = "CRITICAL"
        return out

    def as_dict(self, scale: ScoreScale | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "raw": {"MEDIUM": self.medium, "HIGH": self.high, "CRITICAL": self.critical},
            "recall_targets_on_validation": dict(RECALL_TARGETS),
            "rule": "band = highest level whose raw-score cut the score reaches (score >= cut)",
        }
        if scale is not None:
            d["risk_score"] = {
                b: int(scale.risk_score([v])[0])
                for b, v in (
                    ("MEDIUM", self.medium),
                    ("HIGH", self.high),
                    ("CRITICAL", self.critical),
                )
            }
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> BandCuts:
        r = d["raw"]
        return BandCuts(float(r["MEDIUM"]), float(r["HIGH"]), float(r["CRITICAL"]))


def band_report(y: npt.ArrayLike, s: npt.ArrayLike, cuts: BandCuts) -> dict[str, Any]:
    """Per band: cumulative (>= band) precision/recall/flagged share, and exclusive band rates."""
    ya = np.asarray(y, dtype=np.int64)
    sa = np.asarray(s, dtype=np.float64)
    pos = max(int(ya.sum()), 1)
    labels = cuts.band(sa)
    at_or_above: dict[str, Any] = {}
    for b, cut in (("MEDIUM", cuts.medium), ("HIGH", cuts.high), ("CRITICAL", cuts.critical)):
        flag = sa >= cut
        tp = int(np.sum(flag & (ya == 1)))
        at_or_above[b] = {
            "flagged_share": float(flag.mean()),
            "precision": tp / int(flag.sum()) if flag.any() else float("nan"),
            "recall": tp / pos,
        }
    exclusive = {}
    for b in BANDS:
        m = labels == b
        exclusive[b] = {
            "share": float(m.mean()),
            "n": int(m.sum()),
            "failure_rate": float(ya[m].mean()) if m.any() else float("nan"),
        }
    return {"at_or_above": at_or_above, "exclusive": exclusive, "base_rate": float(ya.mean())}
