"""Explanations (spec section 8.8). They describe the **model**, not causation.

* Global: mean |SHAP| per feature (``shap.TreeExplainer``) on a validation sample.
* Local: per build, the top 3 risk-increasing and top 3 risk-reducing contributions, with
  human-readable text built from ``rl_core`` ``HUMAN_LABELS`` and where the value falls in the
  **training** distribution ("Source lines changed is in the top 5% of builds in the training
  data").

Features that share a human label (``src_churn`` and ``log1p_src_churn``; the two time-of-week
components) are reported as one item whose contribution is the sum of its members' SHAP
values; SHAP values are additive, so the group's attribution is exactly that sum.

For XGBoost the local path uses the booster's native TreeSHAP (``pred_contribs=True``), which
returns the same values as ``shap.TreeExplainer`` without importing shap/numba at serving time
(a test asserts the equality). Contributions are in log-odds of the raw model score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from rl_core.features.schema import ALL_FEATURE_NAMES, HUMAN_LABELS

DISCLAIMER: Final = (
    "Contributions show how the model arrived at its score (SHAP values in log-odds). "
    "They describe the model, not the causes of a failure."
)
N_STAT_QUANTILES: Final = 1001
_DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------- training distribution
@dataclass(frozen=True)
class FeatureStats:
    quantiles: tuple[float, ...]  # training quantiles of non-null values (empty if all null)
    null_rate: float
    binary: bool

    def shares(self, value: float) -> tuple[float, float]:
        """(share of training builds below value, share at or below value)."""
        q = np.asarray(self.quantiles)
        n = len(q)
        lt = np.searchsorted(q, value, side="left") / n
        le = np.searchsorted(q, value, side="right") / n
        return float(lt), float(le)


@dataclass(frozen=True)
class BackgroundStats:
    features: dict[str, FeatureStats]
    n_rows: int

    @staticmethod
    def fit(X: pd.DataFrame) -> BackgroundStats:
        feats: dict[str, FeatureStats] = {}
        for name in X.columns:
            v = X[name].to_numpy(dtype=np.float64)
            ok = v[~np.isnan(v)]
            q = (
                tuple(float(x) for x in np.quantile(ok, np.linspace(0, 1, N_STAT_QUANTILES)))
                if ok.size
                else ()
            )
            binary = bool(ok.size) and bool(np.isin(ok, (0.0, 1.0)).all())
            feats[str(name)] = FeatureStats(q, float(np.isnan(v).mean()), binary)
        return BackgroundStats(feats, len(X))

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "features": {
                k: {"quantiles": list(f.quantiles), "null_rate": f.null_rate, "binary": f.binary}
                for k, f in self.features.items()
            },
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> BackgroundStats:
        return BackgroundStats(
            {
                k: FeatureStats(tuple(v["quantiles"]), float(v["null_rate"]), bool(v["binary"]))
                for k, v in d["features"].items()
            },
            int(d["n_rows"]),
        )


# ---------------------------------------------------------------- text
def display_value(feature: str, value: float | None) -> float | None:
    """The value in its natural unit (``log1p_*`` features are shown as the raw count)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if feature.startswith("log1p_"):
        return float(np.expm1(value))
    return float(value)


def _pct(share: float) -> int:
    return max(1, min(100, math.ceil(share * 100 - 1e-9)))


def describe_value(label: str, stats: FeatureStats | None, value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return f"{label} is unknown for this build"
    if stats is None or not stats.quantiles:
        return f"{label} is {value:g}"
    if stats.binary:
        return f"{label}: {'yes' if value >= 0.5 else 'no'}"
    lt, le = stats.shares(value)
    if lt >= 1.0:
        return f"{label} is higher than in any build in the training data"
    if le <= 0.0:
        return f"{label} is lower than in any build in the training data"
    top, bottom = 1.0 - lt, le
    if top <= 0.5:
        return f"{label} is in the top {_pct(top)}% of builds in the training data"
    if bottom <= 0.5:
        return f"{label} is in the bottom {_pct(bottom)}% of builds in the training data"
    return f"{label} is typical of builds in the training data"


def _time_of_week(sin: float | None, cos: float | None) -> str | None:
    if sin is None or cos is None or math.isnan(sin) or math.isnan(cos):
        return None
    how = (math.atan2(sin, cos) % (2 * math.pi)) * 168.0 / (2 * math.pi)
    h = round(how) % 168
    return f"{_DAYS[h // 24]} {h % 24:02d}:00 UTC"


# ---------------------------------------------------------------- feature groups
def feature_groups(features: tuple[str, ...] | list[str]) -> dict[str, list[str]]:
    """Human label → member features (in model order)."""
    groups: dict[str, list[str]] = {}
    for f in features:
        groups.setdefault(HUMAN_LABELS.get(f, f), []).append(f)
    return groups


# ---------------------------------------------------------------- contributions
def contributions(model: Any, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Per-row, per-feature contributions and the base value, in the model's margin space."""
    from rl_ml.models import SklearnModel, XGBModel

    if isinstance(model, XGBModel):
        import xgboost as xgb

        dm = xgb.DMatrix(X, feature_names=list(X.columns))
        c = model.booster.predict(dm, pred_contribs=True)
        return np.asarray(c[:, :-1], dtype=np.float64), np.asarray(c[:, -1], dtype=np.float64)
    if isinstance(model, SklearnModel) and hasattr(model.estimator, "named_steps"):
        return _linear_contributions(model.estimator, X)
    if isinstance(model, SklearnModel):
        import shap

        expl = shap.TreeExplainer(model.estimator)
        sv = np.asarray(expl.shap_values(X))
        sv = sv[..., 1] if sv.ndim == 3 else sv
        base = np.full(len(X), float(np.ravel(expl.expected_value)[-1]))
        return sv, base
    raise TypeError(f"no explanation method for {type(model).__name__}")


def _linear_contributions(pipe: Any, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Exact log-odds contributions of an imputer → scaler → logistic-regression pipeline."""
    imp, scaler, lr = (
        pipe.named_steps[k] for k in ("simpleimputer", "standardscaler", "logisticregression")
    )
    Z = scaler.transform(imp.transform(X))
    coef = lr.coef_.ravel()
    centred = Z * coef  # each column relative to the training mean (which maps to 0)
    n_feat = X.shape[1]
    contrib = centred[:, :n_feat].copy()
    if getattr(imp, "indicator_", None) is not None:
        for j, src in enumerate(imp.indicator_.features_):
            contrib[:, src] += centred[:, n_feat + j]
    return contrib, np.full(len(X), float(lr.intercept_[0]))


def global_importance(model: Any, X_sample: pd.DataFrame) -> list[dict[str, Any]]:
    """Mean |SHAP| per feature (TreeExplainer for tree models, exact linear terms otherwise)."""
    from rl_ml.models import XGBModel

    if isinstance(model, XGBModel):
        import shap

        sv = np.asarray(shap.TreeExplainer(model.estimator).shap_values(X_sample))
    else:
        sv, _ = contributions(model, X_sample)
    mean_abs = np.abs(sv).mean(axis=0)
    rows = [
        {"feature": f, "label": HUMAN_LABELS.get(f, f), "mean_abs_shap": float(v)}
        for f, v in zip(X_sample.columns, mean_abs, strict=True)
    ]
    return sorted(rows, key=lambda r: -r["mean_abs_shap"])


def explain_rows(
    contrib: npt.NDArray[np.float64],
    X: pd.DataFrame,
    stats: BackgroundStats,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Top ``top_k`` risk-increasing and risk-reducing items per row, with text."""
    features = list(X.columns)
    groups = feature_groups(features)
    col = {f: i for i, f in enumerate(features)}
    values = X.to_numpy(dtype=np.float64)
    out = []
    for r in range(len(X)):
        items: list[dict[str, Any]] = []
        for label, members in groups.items():
            c = float(sum(contrib[r, col[m]] for m in members))
            rep = members[0]
            raw = values[r, col[rep]]
            if members[0].startswith("hour_of_week"):
                sin = (
                    values[r, col["hour_of_week_sin"]]
                    if "hour_of_week_sin" in col
                    else float("nan")
                )
                cos = (
                    values[r, col["hour_of_week_cos"]]
                    if "hour_of_week_cos" in col
                    else float("nan")
                )
                when = _time_of_week(sin, cos)
                text = f"Started {when}" if when else "Start time is unknown"
                value: float | None = None
            else:
                value = display_value(rep, raw)
                text = describe_value(label, stats.features.get(rep), raw)
            items.append(
                {
                    "feature": rep,
                    "features": members,
                    "label": label,
                    "value": value,
                    "contribution": c,
                    "text": text,
                }
            )
        inc = sorted((i for i in items if i["contribution"] > 0), key=lambda i: -i["contribution"])[
            :top_k
        ]
        red = sorted((i for i in items if i["contribution"] < 0), key=lambda i: i["contribution"])[
            :top_k
        ]
        out.append({"top_increasing": inc, "top_reducing": red})
    return out


__all__ = [
    "ALL_FEATURE_NAMES",
    "DISCLAIMER",
    "BackgroundStats",
    "FeatureStats",
    "contributions",
    "describe_value",
    "explain_rows",
    "global_importance",
]
