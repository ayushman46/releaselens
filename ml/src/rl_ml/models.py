"""Candidate models (spec section 8.5). Every candidate exposes ``score(X) -> raw score``.

1. Majority-class baseline: a constant score (the training failure rate).
2. Heuristic rule: ``prev_build_failed OR total_churn > p90(train total_churn)``.
3. Logistic regression: median imputation + missing indicators, standardisation,
   ``class_weight="balanced"``; ``C`` picked on validation PR-AUC from a small grid.
4. Random forest: native NaN support, ``class_weight="balanced_subsample"``;
   ``min_samples_leaf`` picked on validation PR-AUC.
5. XGBoost: ``scale_pos_weight = neg/pos``, early stopping on validation ``aucpr``, Optuna TPE
   (seeded) over the usual tree hyper-parameters. Searched twice, without and with the opt-in
   time-of-week features; the time features are kept only if they improve validation PR-AUC.

Raw scores are *not* probabilities (class weighting inflates them); see :mod:`rl_ml.calibrate`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_NAMES
from rl_ml.data import Mart
from rl_ml.evaluate import pr_auc

log = logging.getLogger("rl_ml.models")

DEFAULT_TRIALS = 30
N_JOBS = 8  # fixed thread count: XGBoost's hist results are reproducible for a fixed nthread


# ---------------------------------------------------------------- baselines
@dataclass
class MajorityClassModel:
    rate: float = 0.0

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> MajorityClassModel:
        self.rate = float(np.mean(y))
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return np.full(len(X), self.rate)


@dataclass
class HeuristicRuleModel:
    """``prev_build_failed == 1 OR total_churn > p90(train)``; NaN inputs count as False."""

    churn_p90: float = float("nan")

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> HeuristicRuleModel:
        churn = X["total_churn"].to_numpy(dtype=np.float64)
        self.churn_p90 = float(np.nanquantile(churn, 0.9))
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        prev = np.nan_to_num(X["prev_build_failed"].to_numpy(dtype=np.float64), nan=0.0) >= 0.5
        churn = X["total_churn"].to_numpy(dtype=np.float64)
        big = np.nan_to_num(churn, nan=-np.inf) > self.churn_p90
        return np.asarray((prev | big), dtype=np.float64)


# ---------------------------------------------------------------- sklearn wrappers
@dataclass
class SklearnModel:
    estimator: Any

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> SklearnModel:
        self.estimator.fit(X, y)
        return self

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.estimator.predict_proba(X)[:, 1], dtype=np.float64)


def make_logreg(C: float, seed: int) -> Any:
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(
        SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
        StandardScaler(),
        LogisticRegression(C=C, class_weight="balanced", max_iter=2000, random_state=seed),
    )


def make_forest(min_samples_leaf: int, seed: int, n_estimators: int = 300) -> Any:
    from sklearn.ensemble import RandomForestClassifier

    return RandomForestClassifier(
        n_estimators=n_estimators,
        min_samples_leaf=min_samples_leaf,
        max_features="sqrt",
        class_weight="balanced_subsample",
        n_jobs=-1,
        random_state=seed,
    )


# ---------------------------------------------------------------- xgboost
@dataclass
class XGBModel:
    """A fitted ``XGBClassifier`` trimmed to its best iteration (no early-stopping state)."""

    estimator: Any

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.estimator.predict_proba(X)[:, 1], dtype=np.float64)

    @property
    def booster(self) -> Any:
        return self.estimator.get_booster()


def _xgb(
    params: dict[str, Any], *, spw: float, seed: int, n_estimators: int, early: int | None
) -> Any:
    import xgboost as xgb

    return xgb.XGBClassifier(
        n_estimators=n_estimators,
        objective="binary:logistic",
        eval_metric="aucpr",
        early_stopping_rounds=early,
        tree_method="hist",
        scale_pos_weight=spw,
        random_state=seed,
        n_jobs=N_JOBS,
        **params,
    )


def search_xgboost(
    Xtr: pd.DataFrame,
    ytr: np.ndarray,
    Xva: pd.DataFrame,
    yva: np.ndarray,
    *,
    trials: int,
    seed: int,
    on_trial: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[XGBModel, dict[str, Any], list[dict[str, Any]]]:
    """Optuna TPE search; returns the refit best model, its params and the trial history."""
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    pos = float(np.sum(ytr))
    spw = (len(ytr) - pos) / max(pos, 1.0)
    history: list[dict[str, Any]] = []

    def objective(trial: optuna.Trial) -> float:
        params = {
            "max_depth": trial.suggest_int("max_depth", 3, 10),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "min_child_weight": trial.suggest_float("min_child_weight", 1.0, 50.0, log=True),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.4, 1.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 30.0, log=True),
            "reg_alpha": trial.suggest_float("reg_alpha", 1e-3, 10.0, log=True),
            "gamma": trial.suggest_float("gamma", 0.0, 5.0),
        }
        model = _xgb(params, spw=spw, seed=seed, n_estimators=2000, early=50)
        model.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
        value = pr_auc(yva, model.predict_proba(Xva)[:, 1])
        best_it = int(model.best_iteration)
        trial.set_user_attr("best_iteration", best_it)
        rec = {"trial": trial.number, "val_pr_auc": value, "best_iteration": best_it} | params
        history.append(rec)
        if on_trial:
            on_trial(rec)
        return value

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(objective, n_trials=trials, show_progress_bar=False)
    best = dict(study.best_trial.params)
    n_trees = int(study.best_trial.user_attrs["best_iteration"]) + 1
    final = _xgb(best, spw=spw, seed=seed, n_estimators=n_trees, early=None)
    final.fit(Xtr, ytr, verbose=False)
    params = best | {"n_estimators": n_trees, "scale_pos_weight": spw}
    return XGBModel(final), params, history


# ---------------------------------------------------------------- candidates
@dataclass
class Candidate:
    name: str
    features: tuple[str, ...]
    model: Any
    params: dict[str, Any]
    val_pr_auc: float
    val_scores: np.ndarray = field(repr=False)
    fit_seconds: float
    search_history: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.model.score(X.loc[:, list(self.features)]), dtype=np.float64)

    @property
    def family(self) -> str:
        return "xgboost" if self.name.startswith("xgboost") else self.name


@dataclass
class TrainingOutcome:
    candidates: list[Candidate]
    time_features_used: bool
    time_feature_decision: dict[str, Any]

    def by_name(self, name: str) -> Candidate:
        return next(c for c in self.candidates if c.name == name)

    @property
    def eligible(self) -> list[Candidate]:
        """Candidates in the final selection (the losing XGBoost time variant is excluded)."""
        drop = "xgboost" if self.time_features_used else "xgboost_time"
        return [c for c in self.candidates if c.name != drop]

    def select(self) -> Candidate:
        """Final model = highest validation PR-AUC (ties: simpler model first, list order)."""
        return max(self.eligible, key=lambda c: c.val_pr_auc)


def _fit_simple(
    name: str, model: Any, feats: tuple[str, ...], train: Mart, val: Mart, params: dict[str, Any]
) -> Candidate:
    t0 = time.perf_counter()
    model.fit(train.X(feats), train.y)
    secs = time.perf_counter() - t0
    vs = model.score(val.X(feats))
    return Candidate(name, feats, model, params, pr_auc(val.y, vs), vs, secs)


def _grid(
    name: str,
    build: Callable[[Any], Any],
    grid: list[Any],
    key: str,
    feats: tuple[str, ...],
    train: Mart,
    val: Mart,
) -> Candidate:
    best: Candidate | None = None
    history = []
    for value in grid:
        cand = _fit_simple(name, SklearnModel(build(value)), feats, train, val, {key: value})
        history.append({key: value, "val_pr_auc": cand.val_pr_auc, "fit_seconds": cand.fit_seconds})
        log.info("%s %s=%s val PR-AUC %.4f", name, key, value, cand.val_pr_auc)
        if best is None or cand.val_pr_auc > best.val_pr_auc:
            best = cand
    assert best is not None
    best.search_history = history
    return best


_XGB_SEARCHED = (
    "max_depth",
    "learning_rate",
    "min_child_weight",
    "subsample",
    "colsample_bytree",
    "reg_lambda",
    "reg_alpha",
    "gamma",
)


def refit(c: Candidate, train: Mart, val: Mart, *, seed: int) -> Candidate:
    """Same family and hyper-parameters, fitted on other rows (cross-project experiment).

    XGBoost re-runs early stopping on the given validation rows; nothing is re-searched.
    """
    feats = c.features
    if c.name == "majority_class":
        return _fit_simple(c.name, MajorityClassModel(), feats, train, val, {})
    if c.name == "heuristic_rule":
        return _fit_simple(c.name, HeuristicRuleModel(), feats, train, val, {})
    if c.name == "logistic_regression":
        return _fit_simple(
            c.name, SklearnModel(make_logreg(c.params["C"], seed)), feats, train, val, c.params
        )
    if c.name == "random_forest":
        model = SklearnModel(make_forest(c.params["min_samples_leaf"], seed))
        return _fit_simple(c.name, model, feats, train, val, c.params)
    params = {k: c.params[k] for k in _XGB_SEARCHED}
    pos = float(train.y.sum())
    spw = (len(train.y) - pos) / max(pos, 1.0)
    t0 = time.perf_counter()
    es = _xgb(params, spw=spw, seed=seed, n_estimators=2000, early=50)
    es.fit(train.X(feats), train.y, eval_set=[(val.X(feats), val.y)], verbose=False)
    n_trees = int(es.best_iteration) + 1
    final = _xgb(params, spw=spw, seed=seed, n_estimators=n_trees, early=None)
    final.fit(train.X(feats), train.y, verbose=False)
    xmodel = XGBModel(final)
    vs = xmodel.score(val.X(feats))
    return Candidate(
        c.name,
        feats,
        xmodel,
        params | {"n_estimators": n_trees, "scale_pos_weight": spw},
        pr_auc(val.y, vs),
        vs,
        time.perf_counter() - t0,
    )


def _named(
    cb: Callable[[str, dict[str, Any]], None] | None, name: str
) -> Callable[[dict[str, Any]], None] | None:
    if cb is None:
        return None

    def bound(rec: dict[str, Any]) -> None:
        cb(name, rec)

    return bound


def train_candidates(
    train: Mart,
    val: Mart,
    *,
    trials: int = DEFAULT_TRIALS,
    seed: int = 42,
    families: tuple[str, ...] = (
        "majority_class",
        "heuristic_rule",
        "logistic_regression",
        "random_forest",
        "xgboost",
    ),
    on_trial: Callable[[str, dict[str, Any]], None] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> TrainingOutcome:
    """Fit every candidate; ``exclude`` drops features (e.g. all-null in train) from all inputs."""
    feats = tuple(f for f in FEATURE_NAMES if f not in exclude)
    feats_time = tuple(f for f in ALL_FEATURE_NAMES if f not in exclude)
    out: list[Candidate] = []
    if "majority_class" in families:
        out.append(_fit_simple("majority_class", MajorityClassModel(), feats, train, val, {}))
    if "heuristic_rule" in families:
        h = _fit_simple("heuristic_rule", HeuristicRuleModel(), feats, train, val, {})
        h.params = {"total_churn_p90_train": h.model.churn_p90}
        out.append(h)
    if "logistic_regression" in families:
        out.append(
            _grid(
                "logistic_regression",
                lambda c: make_logreg(c, seed),
                [0.01, 0.1, 1.0],
                "C",
                feats,
                train,
                val,
            )
        )
    if "random_forest" in families:
        out.append(
            _grid(
                "random_forest",
                lambda m: make_forest(m, seed),
                [5, 25],
                "min_samples_leaf",
                feats,
                train,
                val,
            )
        )
    decision: dict[str, Any] = {"rule": "keep time features only if validation PR-AUC improves"}
    time_used = False
    if "xgboost" in families:
        for name, fs in (("xgboost", feats), ("xgboost_time", feats_time)):
            t0 = time.perf_counter()
            model, params, hist = search_xgboost(
                train.X(fs),
                train.y,
                val.X(fs),
                val.y,
                trials=trials,
                seed=seed,
                on_trial=_named(on_trial, name),
            )
            secs = time.perf_counter() - t0
            vs = model.score(val.X(fs))
            cand = Candidate(name, fs, model, params, pr_auc(val.y, vs), vs, secs, hist)
            log.info("%s val PR-AUC %.4f (%d trials, %.0fs)", name, cand.val_pr_auc, trials, secs)
            out.append(cand)
        no_t, with_t = out[-2].val_pr_auc, out[-1].val_pr_auc
        time_used = with_t > no_t
        decision |= {
            "val_pr_auc_without": no_t,
            "val_pr_auc_with": with_t,
            "delta": with_t - no_t,
            "time_features_used": time_used,
            "trials_each": trials,
        }
    return TrainingOutcome(out, time_used, decision)
