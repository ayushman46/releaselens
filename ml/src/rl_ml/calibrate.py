"""Probability calibration and the ADR-0012 gate (spec section 8.7).

Isotonic regression and Platt scaling are both fitted on the **validation** split only. Which
of the two is *eligible* for the gate is decided on validation as well (5-fold cross-validated
Brier score inside the validation split), so the test set never chooses between them. The
chosen calibrator is then checked once on the untouched test set against thresholds fixed in
ADR-0012 before any result was seen:

* ECE (10 equal-mass bins) <= 0.05
* Brier skill score vs the base-rate predictor > 0 (reference = the test set's own failure
  rate, the stricter choice)
* calibration slope (logistic recalibration) in [0.8, 1.2]

Only when all three hold is ``probability`` exposed; otherwise the API returns ``null``.
Calibrators serialise to plain JSON (no pickles).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

import numpy as np
import numpy.typing as npt

from rl_ml.evaluate import (
    brier,
    brier_skill_score,
    calibration_slope,
    expected_calibration_error,
    fit_logistic_1d,
    logit,
    reliability_curve,
)

ECE_MAX: Final = 0.05
BSS_MIN_EXCLUSIVE: Final = 0.0
SLOPE_RANGE: Final = (0.8, 1.2)
Method = Literal["isotonic", "platt"]


@dataclass(frozen=True)
class Calibrator:
    method: Method
    x: tuple[float, ...] = ()  # isotonic thresholds (raw score)
    y: tuple[float, ...] = ()  # isotonic values (probability)
    a: float = 0.0  # Platt intercept
    b: float = 1.0  # Platt slope on logit(raw)

    def predict(self, raw: npt.ArrayLike) -> np.ndarray:
        r = np.asarray(raw, dtype=np.float64)
        if self.method == "isotonic":
            # identical to sklearn IsotonicRegression(out_of_bounds="clip").predict
            return np.asarray(
                np.interp(r, np.asarray(self.x), np.asarray(self.y)), dtype=np.float64
            )
        z = self.a + self.b * logit(r)
        return 1.0 / (1.0 + np.exp(-z))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["x"], d["y"] = list(self.x), list(self.y)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Calibrator:
        return Calibrator(
            method=d["method"],
            x=tuple(float(v) for v in d.get("x", ())),
            y=tuple(float(v) for v in d.get("y", ())),
            a=float(d.get("a", 0.0)),
            b=float(d.get("b", 1.0)),
        )


def fit_isotonic(raw: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0, increasing=True)
    iso.fit(np.asarray(raw, dtype=np.float64), np.asarray(y, dtype=np.float64))
    return Calibrator(
        "isotonic",
        x=tuple(float(v) for v in iso.X_thresholds_),
        y=tuple(float(v) for v in iso.y_thresholds_),
    )


def fit_platt(raw: npt.ArrayLike, y: npt.ArrayLike) -> Calibrator:
    a, b = fit_logistic_1d(logit(raw), y)
    return Calibrator("platt", a=a, b=b)


FITTERS: Final = {"isotonic": fit_isotonic, "platt": fit_platt}


def select_method(
    raw_val: npt.ArrayLike, y_val: npt.ArrayLike, folds: int = 5, seed: int = 0
) -> dict[str, Any]:
    """Pick isotonic vs Platt by cross-validated Brier score *within validation*."""
    from sklearn.model_selection import StratifiedKFold

    r = np.asarray(raw_val, dtype=np.float64)
    yv = np.asarray(y_val, dtype=np.int64)
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    scores: dict[str, list[float]] = {m: [] for m in FITTERS}
    for tr, te in skf.split(r, yv):
        for m, fit in FITTERS.items():
            cal = fit(r[tr], yv[tr])
            scores[m].append(brier(yv[te], cal.predict(r[te])))
    mean = {m: float(np.mean(v)) for m, v in scores.items()}
    selected = min(mean, key=lambda m: (mean[m], m))
    return {"selected": selected, "cv_brier": mean, "folds": folds, "seed": seed}


def gate(
    y: npt.ArrayLike, p: npt.ArrayLike, *, validation_base_rate: float | None = None
) -> dict[str, Any]:
    """Evaluate the ADR-0012 criteria on (test) labels and calibrated probabilities."""
    ece = expected_calibration_error(y, p, 10)
    bss = brier_skill_score(y, p)  # reference: observed rate of this set (stricter)
    cs = calibration_slope(y, p)
    checks = {
        "ece_le_0.05": ece <= ECE_MAX,
        "brier_skill_gt_0": bss > BSS_MIN_EXCLUSIVE,
        "slope_in_0.8_1.2": SLOPE_RANGE[0] <= cs["slope"] <= SLOPE_RANGE[1],
    }
    out: dict[str, Any] = {
        "ece_10_equal_mass": ece,
        "brier": brier(y, p),
        "brier_skill_score": bss,
        "calibration_slope": cs["slope"],
        "calibration_intercept": cs["intercept"],
        "checks": checks,
        "passed": all(checks.values()),
        "thresholds": {
            "ece_max": ECE_MAX,
            "bss_min_exclusive": BSS_MIN_EXCLUSIVE,
            "slope_range": list(SLOPE_RANGE),
        },
        "reliability": reliability_curve(y, p, 10),
    }
    if validation_base_rate is not None:
        out["brier_skill_score_vs_validation_rate"] = brier_skill_score(y, p, validation_base_rate)
    return out
