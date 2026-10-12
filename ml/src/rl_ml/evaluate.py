"""Evaluation metrics (spec section 8.6). Pure numpy/sklearn; every function is deterministic.

PR-AUC is computed as **average precision** (sklearn ``average_precision_score``), the standard
step-wise estimator of the area under the precision-recall curve. It is the primary metric
because failures are the minority class.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt
from sklearn.metrics import average_precision_score, roc_auc_score

ArrayF = npt.NDArray[np.float64]
_EPS = 1e-6


def _arr(y: npt.ArrayLike, s: npt.ArrayLike) -> tuple[np.ndarray, ArrayF]:
    ya = np.asarray(y, dtype=np.int64).reshape(-1)
    sa = np.asarray(s, dtype=np.float64).reshape(-1)
    if ya.shape != sa.shape:
        raise ValueError(f"y has {ya.size} values, scores {sa.size}")
    if np.isnan(sa).any():
        raise ValueError("scores contain NaN")
    return ya, sa


def pr_auc(y: npt.ArrayLike, s: npt.ArrayLike) -> float:
    ya, sa = _arr(y, s)
    return float(average_precision_score(ya, sa))


def roc_auc(y: npt.ArrayLike, s: npt.ArrayLike) -> float:
    ya, sa = _arr(y, s)
    if ya.min() == ya.max():
        return float("nan")
    return float(roc_auc_score(ya, sa))


def brier(y: npt.ArrayLike, p: npt.ArrayLike) -> float:
    ya, pa = _arr(y, p)
    return float(np.mean((pa - ya) ** 2))


def brier_skill_score(
    y: npt.ArrayLike, p: npt.ArrayLike, reference_rate: float | None = None
) -> float:
    """1 - Brier(model) / Brier(constant ``reference_rate``); default reference = observed rate."""
    ya, pa = _arr(y, p)
    rate = float(ya.mean()) if reference_rate is None else float(reference_rate)
    ref = float(np.mean((rate - ya) ** 2))
    return float("nan") if ref == 0 else 1.0 - brier(ya, pa) / ref


# ---------------------------------------------------------------- operating points
def threshold_metrics(y: npt.ArrayLike, s: npt.ArrayLike, threshold: float) -> dict[str, Any]:
    """Precision / recall / F1 / confusion matrix for ``score >= threshold``."""
    ya, sa = _arr(y, s)
    pred = sa >= threshold
    tp = int(np.sum(pred & (ya == 1)))
    fp = int(np.sum(pred & (ya == 0)))
    fn = int(np.sum(~pred & (ya == 1)))
    tn = int(np.sum(~pred & (ya == 0)))
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if tp else 0.0
    return {
        "threshold": float(threshold),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "flagged_share": float(pred.mean()) if pred.size else float("nan"),
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def best_f1_threshold(y: npt.ArrayLike, s: npt.ArrayLike) -> float:
    """Score threshold maximising F1 (used on validation only)."""
    ya, sa = _arr(y, s)
    order = np.argsort(-sa, kind="stable")
    ss, yy = sa[order], ya[order]
    tp = np.cumsum(yy)
    k = np.arange(1, len(yy) + 1)
    # evaluate only at the last index of each tied score block
    last = np.r_[ss[1:] != ss[:-1], True]
    pos = max(int(ya.sum()), 1)
    f1 = 2 * tp / (k + pos)
    f1 = np.where(last, f1, -1.0)
    i = int(np.argmax(f1))
    return float(ss[i])


def precision_at_top(y: npt.ArrayLike, s: npt.ArrayLike, fraction: float = 0.1) -> float:
    ya, sa = _arr(y, s)
    k = max(1, math.ceil(len(ya) * fraction))
    order = np.argsort(-sa, kind="stable")
    return float(ya[order[:k]].mean())


def lift_at_top(y: npt.ArrayLike, s: npt.ArrayLike, fraction: float = 0.1) -> float:
    ya, sa = _arr(y, s)
    base = float(ya.mean())
    return float("nan") if base == 0 else precision_at_top(ya, sa, fraction) / base


# ---------------------------------------------------------------- calibration
def _equal_mass_bins(p: ArrayF, n_bins: int) -> list[np.ndarray]:
    order = np.argsort(p, kind="stable")
    return [b for b in np.array_split(order, n_bins) if b.size]


def reliability_curve(
    y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10
) -> list[dict[str, float]]:
    """Equal-mass bins: mean predicted probability vs observed failure rate."""
    ya, pa = _arr(y, p)
    return [
        {
            "p_mean": float(pa[b].mean()),
            "observed_rate": float(ya[b].mean()),
            "n": int(b.size),
            "p_min": float(pa[b].min()),
            "p_max": float(pa[b].max()),
        }
        for b in _equal_mass_bins(pa, n_bins)
    ]


def expected_calibration_error(y: npt.ArrayLike, p: npt.ArrayLike, n_bins: int = 10) -> float:
    """ECE over ``n_bins`` equal-mass bins: sum_b (n_b / n) * |mean(p_b) - mean(y_b)|."""
    ya, pa = _arr(y, p)
    n = len(ya)
    return float(
        sum(b.size / n * abs(pa[b].mean() - ya[b].mean()) for b in _equal_mass_bins(pa, n_bins))
    )


def logit(p: npt.ArrayLike, eps: float = _EPS) -> ArrayF:
    pa = np.clip(np.asarray(p, dtype=np.float64), eps, 1 - eps)
    return np.log(pa / (1 - pa))


def fit_logistic_1d(x: npt.ArrayLike, y: npt.ArrayLike, max_iter: int = 100) -> tuple[float, float]:
    """Unpenalised logistic regression ``P(y=1) = sigmoid(a + b x)`` by Newton-Raphson.

    Returns ``(a, b)``. Used for Platt scaling and for the calibration slope.
    """
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    X = np.column_stack([np.ones_like(xa), xa])
    beta = np.zeros(2)
    for _ in range(max_iter):
        z = X @ beta
        mu = 1.0 / (1.0 + np.exp(-z))
        w = np.maximum(mu * (1 - mu), 1e-12)
        grad = X.T @ (ya - mu)
        hess = (X * w[:, None]).T @ X + 1e-10 * np.eye(2)
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(beta[0]), float(beta[1])


def calibration_slope(y: npt.ArrayLike, p: npt.ArrayLike, eps: float = 1e-4) -> dict[str, float]:
    """Logistic recalibration ``y ~ a + b * logit(p)``; perfect calibration has a=0, b=1.

    Probabilities are clipped to ``[eps, 1-eps]`` before the logit so a few exact 0/1 outputs
    (possible with isotonic regression) cannot dominate the fit.
    """
    ya, pa = _arr(y, p)
    a, b = fit_logistic_1d(logit(pa, eps), ya)
    return {"intercept": a, "slope": b}


# ---------------------------------------------------------------- groups
def per_project_pr_auc(
    y: npt.ArrayLike,
    s: npt.ArrayLike,
    projects: npt.ArrayLike,
    min_builds: int = 30,
) -> dict[str, Any]:
    """PR-AUC per project with >= ``min_builds`` rows, >= 1 positive and >= 1 negative.

    (A project with no negatives has a trivial PR-AUC of 1.0, so it is excluded too.)
    """
    ya, sa = _arr(y, s)
    pa = np.asarray(projects).astype(str)
    rows: list[dict[str, Any]] = []
    for proj in np.unique(pa):
        m = pa == proj
        yy, ss = ya[m], sa[m]
        pos = int(yy.sum())
        if m.sum() < min_builds or pos == 0 or pos == yy.size:
            continue
        ap = float(average_precision_score(yy, ss))
        base = pos / yy.size
        rows.append(
            {
                "project": str(proj),
                "n": int(yy.size),
                "positives": pos,
                "base_rate": base,
                "pr_auc": ap,
                "lift": ap / base,
            }
        )
    aps = np.array([r["pr_auc"] for r in rows])
    lifts = np.array([r["lift"] for r in rows])
    summary: dict[str, Any] = {
        "min_builds": min_builds,
        "projects_eligible": len(rows),
        "projects_total": len(np.unique(pa)),
    }
    if rows:
        summary |= {
            "pr_auc_median": float(np.median(aps)),
            "pr_auc_p25": float(np.quantile(aps, 0.25)),
            "pr_auc_p75": float(np.quantile(aps, 0.75)),
            "lift_median": float(np.median(lifts)),
            "share_above_base_rate": float(np.mean(lifts > 1.0)),
        }
    return {"summary": summary, "projects": sorted(rows, key=lambda r: -r["n"])}


def cluster_bootstrap_ci(
    y: npt.ArrayLike,
    s: npt.ArrayLike,
    groups: npt.ArrayLike,
    metric: str = "pr_auc",
    n_resamples: int = 500,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, float]:
    """Percentile CI from resampling whole projects (builds within a project are correlated)."""
    ya, sa = _arr(y, s)
    ga = np.asarray(groups).astype(str)
    fn = {"pr_auc": average_precision_score, "roc_auc": roc_auc_score}[metric]
    uniq, inv = np.unique(ga, return_inverse=True)
    idx_by_group = [np.flatnonzero(inv == g) for g in range(len(uniq))]
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_resamples):
        pick = rng.integers(0, len(uniq), size=len(uniq))
        idx = np.concatenate([idx_by_group[g] for g in pick])
        yy = ya[idx]
        if yy.min() == yy.max():
            continue
        vals.append(float(fn(yy, sa[idx])))
    alpha = 1 - confidence
    lo, hi = np.quantile(vals, [alpha / 2, 1 - alpha / 2])
    return {
        "low": float(lo),
        "high": float(hi),
        "confidence": confidence,
        "n_resamples": len(vals),
        "seed": seed,
    }


# ---------------------------------------------------------------- bundle
def summarize(
    y: npt.ArrayLike,
    s: npt.ArrayLike,
    *,
    threshold: float | None = None,
    top_fraction: float = 0.1,
) -> dict[str, Any]:
    """Ranking metrics (+ operating-point metrics when ``threshold`` is given)."""
    ya, sa = _arr(y, s)
    base = float(ya.mean())
    out: dict[str, Any] = {
        "n": int(ya.size),
        "positives": int(ya.sum()),
        "base_rate": base,
        "pr_auc": pr_auc(ya, sa),
        "roc_auc": roc_auc(ya, sa),
        "precision_at_top_10pct": precision_at_top(ya, sa, top_fraction),
        "lift_at_top_10pct": lift_at_top(ya, sa, top_fraction),
        "pr_auc_over_base_rate": pr_auc(ya, sa) / base if base else float("nan"),
    }
    if threshold is not None:
        out["at_threshold"] = threshold_metrics(ya, sa, threshold)
    return out
