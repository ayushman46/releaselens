"""Training pipeline and CLI (spec section 8).

    python -m rl_ml.train --features-run-id <run_id> [--errored-excluded] [--trials 30]
    python -m rl_ml.train --latest                      # newest mart run in the object store
    python -m rl_ml.train --synthetic --no-mlflow       # SYNTHETIC smoke run (never in docs)

Order of operations (each step only sees what it is allowed to see):

1. load + validate the mart, leakage audit (abort on any violation);
2. fit every candidate on **train**, tune/early-stop on **validation**;
3. lock the final model by validation PR-AUC (time features only if they helped);
4. on **validation** only: decision threshold (max F1), risk-score quantile table, band
   cut-points, calibrators (isotonic + Platt) and the choice between them;
5. open the **test** set exactly once (guarded) and compute every test number in that call;
6. cross-project experiment on held-out projects (its own once-only guard);
7. explanations, training-distribution statistics, PSI bins; serving bundle; MLflow.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_SCHEMA_VERSION, TIME_FEATURES
from rl_ml import plots
from rl_ml.bands import BandCuts, ScoreScale, band_report
from rl_ml.calibrate import fit_isotonic, fit_platt, gate, select_method
from rl_ml.data import (
    DATA_SOURCE,
    Mart,
    class_balance,
    exclude_errored,
    leakage_audit,
    list_feature_runs,
    load_mart,
    make_s3_client,
)
from rl_ml.evaluate import (
    best_f1_threshold,
    cluster_bootstrap_ci,
    per_project_pr_auc,
    roc_auc,
    summarize,
)
from rl_ml.explain import BackgroundStats, contributions, explain_rows, global_importance
from rl_ml.models import Candidate, TrainingOutcome, refit, train_candidates
from rl_ml.monitoring import training_bins
from rl_ml.predictor import RiskModelBundle
from rl_ml.splits import HeldOutSet, chronological_summary, cold_start, cross_project_split

log = logging.getLogger("rl_ml.train")

RESULTS_SCHEMA: Final = "rl_ml.results/1"
VARIANTS: Final = {False: "failed_or_errored", True: "errored_excluded"}
LABEL_DEFINITIONS: Final = {
    "failed_or_errored": (
        "label 1 = failed or errored, 0 = passed; canceled/started excluded (ADR-0005)"
    ),
    "errored_excluded": (
        "label 1 = failed, 0 = passed; errored, canceled and started builds excluded "
        "(ADR-0005 sensitivity analysis)"
    ),
}
DISPLAY: Final = {
    "majority_class": "Majority class",
    "heuristic_rule": "Heuristic rule",
    "logistic_regression": "Logistic regression",
    "random_forest": "Random forest",
    "xgboost": "XGBoost",
    "xgboost_time": "XGBoost + time features",
}
DEFAULT_FAMILIES: Final = (
    "majority_class",
    "heuristic_rule",
    "logistic_regression",
    "random_forest",
    "xgboost",
)


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "ml").is_dir():
            return parent
    return Path.cwd()


@dataclass
class TrainConfig:
    trials: int = 30
    seed: int = 42
    errored_excluded: bool = False
    holdout_fraction: float = 0.2
    min_project_builds: int = 30
    bootstrap_resamples: int = 500
    shap_sample: int = 5000
    families: tuple[str, ...] = DEFAULT_FAMILIES

    @property
    def variant(self) -> str:
        return VARIANTS[self.errored_excluded]


@dataclass
class TrainingRun:
    config: TrainConfig
    results: dict[str, Any]
    final: Candidate
    outcome: TrainingOutcome
    bundle: RiskModelBundle
    test_curves: dict[str, Any] = field(repr=False)
    X_example: pd.DataFrame = field(repr=False)
    guards: list[HeldOutSet] = field(default_factory=list, repr=False)


# ---------------------------------------------------------------- helpers
def clean_json(o: Any) -> Any:
    """NaN/inf → None, numpy → Python, recursively (strict JSON)."""
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [clean_json(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, np.ndarray):
        return clean_json(o.tolist())
    return o


def git_info() -> dict[str, Any]:
    root = repo_root()
    try:
        sha = subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(  # noqa: S603 - fixed argv, no user input
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"sha": None, "dirty": None}
    return {"sha": sha, "dirty": dirty}


def environment() -> dict[str, str]:
    import optuna
    import shap
    import sklearn
    import xgboost

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scikit_learn": sklearn.__version__,
        "xgboost": xgboost.__version__,
        "shap": shap.__version__,
        "optuna": optuna.__version__,
    }


def _candidate_row(c: Candidate, val: Mart) -> dict[str, Any]:
    return {
        "name": c.name,
        "display": DISPLAY.get(c.name, c.name),
        "features": len(c.features),
        "uses_time_features": any(f in TIME_FEATURES for f in c.features),
        "val_pr_auc": c.val_pr_auc,
        "val_roc_auc": roc_auc(val.y, c.val_scores),
        "params": c.params,
        "fit_seconds": c.fit_seconds,
        "search": c.search_history,
    }


# ---------------------------------------------------------------- pipeline
def run_training(mart: Mart, cfg: TrainConfig, on_trial: Any = None) -> TrainingRun:
    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    def lap(name: str, t0: float) -> float:
        now = time.perf_counter()
        timings[name] = round(now - t0, 2)
        return now

    t = time.perf_counter()
    if cfg.errored_excluded:
        mart = exclude_errored(mart)
    audit = leakage_audit(mart, ALL_FEATURE_NAMES)
    balance = class_balance(mart)
    chrono = chronological_summary(mart)
    if not chrono["ordered"]:
        raise ValueError("the mart's chronological split interleaves in time")
    train, val = mart.split("train"), mart.split("validation")
    test_guard = HeldOutSet("chronological_test", mart.split("test"))
    t = lap("load_and_audit", t)

    # ---- 2-3: candidates and selection (validation only)
    outcome = train_candidates(
        train, val, trials=cfg.trials, seed=cfg.seed, families=cfg.families, on_trial=on_trial
    )
    final = outcome.select()
    locked_at = datetime.now(UTC).isoformat()
    t = lap("train_candidates", t)

    # ---- 4: validation-only artefacts
    vs = final.val_scores
    threshold = best_f1_threshold(val.y, vs)
    scale = ScoreScale.fit(vs)
    cuts = BandCuts.fit(val.y, vs)
    cal_choice = select_method(vs, val.y, seed=cfg.seed)
    calibrators = {"isotonic": fit_isotonic(vs, val.y), "platt": fit_platt(vs, val.y)}
    val_base = float(val.y.mean())
    validation = {
        "final": summarize(val.y, vs, threshold=threshold),
        "threshold_rule": "raw-score threshold maximising F1 on validation",
        "bands": band_report(val.y, vs, cuts),
        "calibration_method_selection": cal_choice,
        "calibrated_brier": {
            m: float(np.mean((c.predict(vs) - val.y) ** 2)) for m, c in calibrators.items()
        },
    }
    t = lap("validation_artifacts", t)

    # ---- 5: the single test-set evaluation
    def evaluate_test(test: Mart) -> dict[str, Any]:
        X = test.X(final.features)
        s = final.score(X)
        projects = test.frame["project"].to_numpy()
        out: dict[str, Any] = {"final": summarize(test.y, s, threshold=threshold)}
        out["final"]["pr_auc_ci95_project_bootstrap"] = cluster_bootstrap_ci(
            test.y, s, projects, "pr_auc", cfg.bootstrap_resamples, seed=cfg.seed
        )
        out["final"]["roc_auc_ci95_project_bootstrap"] = cluster_bootstrap_ci(
            test.y, s, projects, "roc_auc", cfg.bootstrap_resamples, seed=cfg.seed
        )
        out["per_project"] = per_project_pr_auc(test.y, s, projects, cfg.min_project_builds)
        out["cold_start"] = summarize(test.y, final.score(cold_start(X)))
        out["cold_start"]["masked_features"] = [f for f in X.columns if f in _cold_masked()]
        sel = cal_choice["selected"]
        gates = {
            m: gate(test.y, c.predict(s), validation_base_rate=val_base)
            for m, c in calibrators.items()
        }
        out["calibration"] = {
            "selected_method": sel,
            "selected_on": "validation (5-fold CV Brier inside the validation split)",
            "gate": gates[sel],
            "other_method_informational": {m: g for m, g in gates.items() if m != sel},
        }
        out["probability_exposed"] = bool(gates[sel]["passed"])
        out["bands"] = band_report(test.y, s, cuts)
        ref_scores = {c.name: c.score(test.X(c.features)) for c in outcome.candidates}
        out["reference_candidates"] = {
            name: summarize(test.y, sc, threshold=None) for name, sc in ref_scores.items()
        }
        out["reference_note"] = (
            "computed in the same single test pass, after the final model was locked on "
            "validation; never used for selection"
        )
        out["_scores"] = {"y": test.y, "final": s, **ref_scores}
        return out

    test_eval = test_guard.evaluate_once(
        evaluate_test, purpose=f"final evaluation of {final.name} (selection locked {locked_at})"
    )
    test_scores = test_eval.pop("_scores")
    t = lap("test_evaluation", t)

    # ---- 6: cross-project generalisation (separate experiment, own guard)
    cp = cross_project_split(mart, cfg.holdout_fraction, cfg.seed)
    cp_model = refit(final, cp.train, cp.validation, seed=cfg.seed)
    cp_heur = (
        refit(outcome.by_name("heuristic_rule"), cp.train, cp.validation, seed=cfg.seed)
        if any(c.name == "heuristic_rule" for c in outcome.candidates)
        else None
    )
    cp_guard = HeldOutSet("cross_project_holdout", cp.test_unseen_all)

    def evaluate_cp(_: Mart) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for key, part in (
            ("unseen_projects_test_window", cp.test_unseen),
            ("unseen_projects_all_rows", cp.test_unseen_all),
            ("seen_projects_test_window", cp.test_seen),
        ):
            if len(part.frame) == 0 or part.y.min() == part.y.max():
                res[key] = {"n": len(part.frame), "note": "too few rows or a single class"}
                continue
            res[key] = summarize(part.y, cp_model.score(part.X(cp_model.features)))
            res[key]["projects"] = int(part.frame["project"].nunique())
            if cp_heur is not None:
                res[key]["heuristic_rule_pr_auc"] = summarize(
                    part.y, cp_heur.score(part.X(cp_heur.features))
                )["pr_auc"]
        return res

    cross = cp_guard.evaluate_once(
        evaluate_cp, purpose=f"cross-project evaluation of refit {final.name}"
    )
    cross_project = {
        "held_out_projects": len(cp.held_out),
        "total_projects": int(mart.frame["project"].nunique()),
        "fraction": cfg.holdout_fraction,
        "selection": f"first {len(cp.held_out)} projects ranked by sha256('{cfg.seed}:<project>')",
        "train_rows": len(cp.train.frame),
        "validation_rows": len(cp.validation.frame),
        "model": final.name,
        "model_params": cp_model.params,
        "val_pr_auc": cp_model.val_pr_auc,
        "results": cross,
    }
    t = lap("cross_project", t)

    # ---- 7: explanations, statistics, bundle
    Xtr = train.X(final.features)
    stats = BackgroundStats.fit(Xtr)
    psi_bins = training_bins(Xtr)
    explain_block: dict[str, Any] = {"global": [], "examples_validation": []}
    try:
        Xv = val.X(final.features)
        sample = Xv.sample(n=min(cfg.shap_sample, len(Xv)), random_state=cfg.seed)
        explain_block["global"] = global_importance(final.model, sample)
        explain_block["global_sample"] = {
            "split": "validation",
            "rows": len(sample),
            "seed": cfg.seed,
        }
        explain_block["global_method"] = (
            "exact linear contributions, log-odds"
            if final.name == "logistic_regression"
            else "shap.TreeExplainer, probability units"
            if final.name == "random_forest"
            else "shap.TreeExplainer, log-odds"
        )
        order = np.argsort(vs, kind="stable")
        picks = {
            "highest_score": int(order[-1]),
            "median_score": int(order[len(order) // 2]),
            "lowest_score": int(order[0]),
        }
        idx = list(picks.values())
        contrib, _ = contributions(final.model, Xv.iloc[idx])
        rows = explain_rows(contrib, Xv.iloc[idx], stats)
        for (kind, i), ex in zip(picks.items(), rows, strict=True):
            explain_block["examples_validation"].append(
                {
                    "kind": kind,
                    "build_id": int(val.frame["build_id"].iloc[i]),
                    "project": str(val.frame["project"].iloc[i]),
                    "risk_score": int(scale.risk_score([vs[i]])[0]),
                    "risk_band": str(cuts.band([vs[i]])[0]),
                    **ex,
                }
            )
    except TypeError as exc:  # baseline final model: no attribution
        explain_block["note"] = str(exc)
    t = lap("explanations", t)

    gate_sel = test_eval["calibration"]["gate"]
    bundle = RiskModelBundle(
        model=final.model,
        features=final.features,
        scale=scale,
        cuts=cuts,
        calibrator=calibrators[cal_choice["selected"]] if gate_sel["passed"] else None,
        calibration={
            "method": cal_choice["selected"],
            **{k: v for k, v in gate_sel.items() if k != "reliability"},
        },
        stats=stats,
        psi_bins=psi_bins,
        meta={
            "model_name": final.name,
            "label_variant": cfg.variant,
            "dataset_md5": mart.dataset_md5,
            "features_run_id": mart.run_id,
            "data_source": mart.source,
            "trained_at": locked_at,
        },
    )

    results: dict[str, Any] = {
        "schema": RESULTS_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "variant": cfg.variant,
        "label_definition": LABEL_DEFINITIONS[cfg.variant],
        "data": {
            "source": mart.source,
            "synthetic": mart.is_synthetic,
            "dataset_md5": mart.dataset_md5,
            "features_run_id": mart.run_id,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "notes": list(mart.notes),
        },
        "config": asdict(cfg) | {"variant": cfg.variant},
        "class_balance": balance,
        "splits": {"chronological": chrono},
        "leakage_audit": audit,
        "candidates": [_candidate_row(c, val) for c in outcome.candidates],
        "time_features": outcome.time_feature_decision,
        "selection": {
            "final_model": final.name,
            "display": DISPLAY.get(final.name, final.name),
            "rule": "highest validation PR-AUC among eligible candidates",
            "eligible": [c.name for c in outcome.eligible],
            "val_pr_auc": final.val_pr_auc,
            "locked_at": locked_at,
            "features": list(final.features),
        },
        "validation": validation,
        "test": {
            "evaluations": len(test_guard.access_log),
            "access_log": test_guard.access_log,
            "rows": test_guard.rows,
            **test_eval,
        },
        "bands": cuts.as_dict(scale),
        "cross_project": cross_project | {"access_log": cp_guard.access_log},
        "explainability": explain_block,
        "git": git_info(),
        "environment": environment(),
        "registry": None,
    }
    timings["total"] = round(time.perf_counter() - t_start, 2)
    results["timings_seconds"] = timings
    return TrainingRun(
        config=cfg,
        results=clean_json(results),
        final=final,
        outcome=outcome,
        bundle=bundle,
        test_curves=test_scores,
        X_example=val.X(final.features).head(5),
        guards=[test_guard, cp_guard],
    )


def _cold_masked() -> tuple[str, ...]:
    from rl_ml.splits import COLD_START_MASKED

    return COLD_START_MASKED


# ---------------------------------------------------------------- outputs
def write_outputs(run: TrainingRun, out_dir: Path) -> dict[str, Path]:
    """results.json, serving bundle and figures under ``out_dir/<variant>/``."""
    d = out_dir / run.config.variant
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    paths: dict[str, Path] = {}
    paths["results"] = d / "results.json"
    paths["results"].write_text(json.dumps(run.results, indent=1, sort_keys=False))
    run.bundle.save(d / "risk_bundle")
    paths["bundle"] = d / "risk_bundle"
    fig = d / "figures"
    tc = run.test_curves
    y = np.asarray(tc["y"])
    final = run.final.name
    lines = {f"{DISPLAY[final]} (final)": np.asarray(tc["final"])}
    for name in ("logistic_regression", "heuristic_rule"):
        if name in tc and name != final:
            lines[DISPLAY[name]] = np.asarray(tc[name])
    variant = run.config.variant
    paths["pr_curve"] = plots.pr_curves(
        y,
        lines,
        float(y.mean()),
        fig / f"{variant}_pr_curve.png",
        f"Precision-recall, test set ({variant})",
    )
    gate_res = run.results["test"]["calibration"]["gate"]
    method = run.results["test"]["calibration"]["selected_method"]
    paths["reliability"] = plots.reliability(
        gate_res["reliability"],
        fig / f"{variant}_reliability.png",
        f"{method} calibrated",
        f"Reliability, test set ({variant})",
    )
    glob = run.results["explainability"]["global"]
    if glob:
        paths["shap"] = plots.importance(glob, fig / f"{variant}_shap_importance.png")
    return paths


def publish_docs(run: TrainingRun, paths: dict[str, Path], docs_dir: Path) -> list[Path]:
    """Copy a REAL run's JSON + figures into docs/ml and regenerate RESULTS.md / MODEL_CARD.md."""
    if run.results["data"]["synthetic"] or run.results["data"]["source"] != DATA_SOURCE:
        raise ValueError("refusing to publish a synthetic run to docs/ml")
    from rl_ml import card

    written = []
    res_dir = docs_dir / "results"
    fig_dir = docs_dir / "figures"
    res_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)
    target = res_dir / f"{run.config.variant}.json"
    target.write_text(json.dumps(run.results, indent=1))
    written.append(target)
    for key in ("pr_curve", "reliability", "shap"):
        if key in paths:
            dst = fig_dir / paths[key].name
            shutil.copyfile(paths[key], dst)
            written.append(dst)
    written += card.write_docs(docs_dir)
    return written


# ---------------------------------------------------------------- CLI
def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m rl_ml.train",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--features-run-id", help="feature mart run_id in the object store")
    src.add_argument("--latest", action="store_true", help="use the newest mart run_id")
    src.add_argument("--synthetic", action="store_true", help="SYNTHETIC data (smoke runs only)")
    p.add_argument("--errored-excluded", action="store_true", help="ADR-0005 sensitivity variant")
    p.add_argument("--trials", type=int, default=30, help="Optuna trials per XGBoost search")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", type=Path, default=repo_root() / "ml" / "artifacts" / "runs")
    p.add_argument("--docs-dir", type=Path, default=repo_root() / "docs" / "ml")
    p.add_argument("--no-docs", action="store_true", help="do not publish JSON/figures/RESULTS.md")
    p.add_argument("--no-mlflow", action="store_true")
    p.add_argument(
        "--tracking-uri",
        default=None,
        help="default: $MLFLOW_TRACKING_URI or http://127.0.0.1:25000",
    )
    reg = p.add_mutually_exclusive_group()
    reg.add_argument("--register", dest="register", action="store_true", default=None)
    reg.add_argument("--no-register", dest="register", action="store_false")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    cfg = TrainConfig(trials=args.trials, seed=args.seed, errored_excluded=args.errored_excluded)

    if args.synthetic:
        from rl_ml.data import prepare_mart
        from rl_ml.synthetic import SYNTHETIC_SOURCE, make_synthetic_mart

        log.warning("SYNTHETIC data: metrics from this run are NOT real results")
        mart = prepare_mart(
            make_synthetic_mart(seed=args.seed),
            run_id=f"synthetic-seed{args.seed}",
            source=SYNTHETIC_SOURCE,
            dataset_md5=None,
        )
    else:
        run_id = args.features_run_id
        if args.latest:
            runs = list_feature_runs(make_s3_client())
            if not runs:
                log.error("no feature mart runs found in the object store")
                return 2
            run_id = runs[-1]
        log.info("loading feature mart run_id=%s", run_id)
        mart = load_mart(run_id)

    trial_log: list[tuple[str, dict[str, Any]]] = []

    def on_trial(name: str, rec: dict[str, Any]) -> None:
        trial_log.append((name, rec))
        log.info(
            "%s trial %d val PR-AUC %.4f (trees %d)",
            name,
            rec["trial"],
            rec["val_pr_auc"],
            rec["best_iteration"] + 1,
        )

    run = run_training(mart, cfg, on_trial=on_trial)
    paths = write_outputs(run, args.out_dir)
    log.info("wrote %s", paths["results"])

    if not args.no_mlflow:
        from rl_ml import registry

        register = (
            args.register
            if args.register is not None
            else (not cfg.errored_excluded and not mart.is_synthetic)
        )
        info = registry.log_training_run(
            run, paths, tracking_uri=args.tracking_uri, register=register
        )
        run.results["registry"] = info
        paths["results"].write_text(json.dumps(run.results, indent=1))
        log.info("mlflow: %s", info)

    if not args.no_docs and not mart.is_synthetic:
        for pth in publish_docs(run, paths, args.docs_dir):
            log.info("docs: %s", pth)

    r = run.results
    sys.stdout.write(
        json.dumps(
            {
                "variant": r["variant"],
                "final_model": r["selection"]["final_model"],
                "val_pr_auc": r["selection"]["val_pr_auc"],
                "test_pr_auc": r["test"]["final"]["pr_auc"],
                "test_base_rate": r["test"]["final"]["base_rate"],
                "probability_exposed": r["test"]["probability_exposed"],
                "synthetic": r["data"]["synthetic"],
                "registry": r["registry"],
            },
            indent=1,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
