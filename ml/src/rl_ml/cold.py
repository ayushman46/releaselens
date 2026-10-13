"""The no-history ("cold start") model served to a repository with no finished builds yet.

M4 found that masking the history features of the champion (a model trained *with* history)
gives fragile, inflated scores: the trees route missing history the way they learned from the
~0.3% of training rows that had none (docs/ml/RESULTS.md §9). A day-one tenant is therefore
served by a model of the same family and hyper-parameters trained **without** history features:
the "no-history model (diagnostic)" of the M4 run, here turned into a full serving bundle with
its own validation-only score scale, bands and calibration, one guarded test evaluation and an
MLflow registration as ``release_risk_ci_cold`` (alias ``champion``).

    python -m rl_ml.cold --features-run-id m3-run1 [--register] [--promote]
    python -m rl_ml.cold --synthetic --no-mlflow          # SYNTHETIC smoke run (never in docs)

Hyper-parameters come from the champion's results JSON (``docs/ml/results/<variant>.json``);
nothing is re-searched, and only early stopping re-runs on validation, exactly like
:func:`rl_ml.models.refit` in the M4 diagnostic.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np

from rl_core.features.schema import FEATURE_SCHEMA_VERSION, HISTORY_FEATURES
from rl_ml.bands import BandCuts, ScoreScale, band_report
from rl_ml.calibrate import fit_isotonic, fit_platt, gate, select_method
from rl_ml.data import DATA_SOURCE, Mart, leakage_audit
from rl_ml.evaluate import best_f1_threshold, roc_auc, summarize
from rl_ml.explain import BackgroundStats
from rl_ml.models import Candidate, refit
from rl_ml.monitoring import training_bins
from rl_ml.predictor import RiskModelBundle
from rl_ml.splits import COLD_START_MASKED, HeldOutSet

log = logging.getLogger("rl_ml.cold")

COLD_MODEL_NAME: Final = "release_risk_ci_cold"
COLD_EXPERIMENT: Final = "release_risk_ci_cold"
RESULTS_SCHEMA: Final = "rl_ml.cold_results/1"
ROLE: Final = "cold_start"


class ColdModelError(RuntimeError):
    pass


@dataclass
class ColdRun:
    results: dict[str, Any]
    candidate: Candidate
    bundle: RiskModelBundle
    guards: list[HeldOutSet] = field(default_factory=list, repr=False)


def champion_spec(results: dict[str, Any]) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    """(family, hyper-parameters, feature list) of the champion run's final model."""
    final = results["selection"]["final_model"]
    row = next((c for c in results["candidates"] if c["name"] == final), None)
    if row is None:
        raise ColdModelError(f"final model {final!r} not among the run's candidates")
    if final in ("majority_class", "heuristic_rule"):
        raise ColdModelError("the champion is a baseline; there is nothing to refit")
    return final, dict(row["params"]), tuple(results["selection"]["features"])


def cold_features(features: tuple[str, ...]) -> tuple[str, ...]:
    """The champion's features minus every history feature (the M4 diagnostic's 26)."""
    out = tuple(f for f in features if f not in COLD_START_MASKED and f not in HISTORY_FEATURES)
    if not out:
        raise ColdModelError("no features left without history")
    return out


def run_cold_training(mart: Mart, champion_results: dict[str, Any], *, seed: int = 42) -> ColdRun:
    """Refit without history, build the bundle, evaluate the test split once."""
    audit = leakage_audit(mart)
    family, params, champ_feats = champion_spec(champion_results)
    feats = cold_features(champ_feats)
    train, val = mart.split("train"), mart.split("validation")
    test_guard = HeldOutSet("chronological_test", mart.split("test"))

    template = Candidate(
        name=family,
        features=feats,
        model=None,
        params=params,
        val_pr_auc=float("nan"),
        val_scores=np.empty(0),
        fit_seconds=0.0,
    )
    cand = refit(template, train, val, seed=seed, features=feats)
    locked_at = datetime.now(UTC).isoformat()

    vs = cand.val_scores
    threshold = best_f1_threshold(val.y, vs)
    scale = ScoreScale.fit(vs)
    cuts = BandCuts.fit(val.y, vs)
    cal_choice = select_method(vs, val.y, seed=seed)
    calibrators = {"isotonic": fit_isotonic(vs, val.y), "platt": fit_platt(vs, val.y)}
    val_base = float(val.y.mean())
    validation = {
        "final": summarize(val.y, vs, threshold=threshold),
        "bands": band_report(val.y, vs, cuts),
        "calibration_method_selection": cal_choice,
    }

    def evaluate_test(test: Mart) -> dict[str, Any]:
        s = cand.score(test.X(feats))
        out: dict[str, Any] = {"final": summarize(test.y, s, threshold=threshold)}
        gates = {
            m: gate(test.y, c.predict(s), validation_base_rate=val_base)
            for m, c in calibrators.items()
        }
        sel = cal_choice["selected"]
        out["calibration"] = {
            "selected_method": sel,
            "selected_on": "validation (5-fold CV Brier inside the validation split)",
            "gate": {k: v for k, v in gates[sel].items() if k != "reliability"},
            "reliability": gates[sel]["reliability"],
            "other_method_informational": {
                m: {k: v for k, v in g.items() if k != "reliability"}
                for m, g in gates.items()
                if m != sel
            },
        }
        out["probability_exposed"] = bool(gates[sel]["passed"])
        out["bands"] = band_report(test.y, s, cuts)
        prev = test.frame["prev_build_failed"].to_numpy(dtype=np.float64)
        slices: dict[str, Any] = {}
        for name, mask in (
            ("previous_passed", prev == 0),
            ("previous_failed", prev == 1),
            ("no_finished_history", np.isnan(prev)),
        ):
            y = test.y[mask]
            if mask.sum() == 0 or y.min() == y.max():
                slices[name] = {"n": int(mask.sum()), "note": "single class or empty"}
            else:
                slices[name] = summarize(y, s[mask]) | {"share_of_rows": float(mask.mean())}
        out["by_previous_outcome"] = slices
        return out

    test_eval = test_guard.evaluate_once(
        evaluate_test,
        purpose=f"single evaluation of the no-history {family} refit (locked {locked_at})",
    )

    Xtr = train.X(feats)
    gate_sel = test_eval["calibration"]["gate"]
    bundle = RiskModelBundle(
        model=cand.model,
        features=feats,
        scale=scale,
        cuts=cuts,
        calibrator=calibrators[cal_choice["selected"]] if gate_sel["passed"] else None,
        calibration={"method": cal_choice["selected"], **gate_sel},
        stats=BackgroundStats.fit(Xtr),
        psi_bins=training_bins(Xtr),
        meta={
            "model_name": family,
            "role": ROLE,
            "label_variant": champion_results.get("variant", "failed_or_errored"),
            "dataset_md5": mart.dataset_md5,
            "features_run_id": mart.run_id,
            "data_source": mart.source,
            "trained_at": locked_at,
            "hyper_parameters_from": (champion_results.get("registry") or {}).get("model_uri")
            or "champion results JSON",
        },
    )
    champ_diag = (champion_results.get("test") or {}).get("cold_start_no_history_model") or {}
    results: dict[str, Any] = {
        "schema": RESULTS_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "role": ROLE,
        "registered_model_name": COLD_MODEL_NAME,
        "variant": champion_results.get("variant", "failed_or_errored"),
        "data": {
            "source": mart.source,
            "synthetic": mart.is_synthetic,
            "dataset_md5": mart.dataset_md5,
            "features_run_id": mart.run_id,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "notes": list(mart.notes),
        },
        "leakage_audit": audit,
        "model": {
            "family": family,
            "features": list(feats),
            "n_features": len(feats),
            "excluded_history_features": [f for f in champ_feats if f not in feats],
            "params": cand.params,
            "hyper_parameters": (
                "the champion's (no re-search); early stopping re-run on validation"
            ),
            "seed": seed,
            "fit_seconds": cand.fit_seconds,
            "locked_at": locked_at,
        },
        "validation": validation
        | {"val_pr_auc": cand.val_pr_auc, "val_roc_auc": roc_auc(val.y, vs)},
        "test": {
            "evaluations": len(test_guard.access_log),
            "access_log": test_guard.access_log,
            "rows": test_guard.rows,
            **test_eval,
        },
        "bands": cuts.as_dict(scale),
        "m4_diagnostic_reference": {
            "test_pr_auc": champ_diag.get("pr_auc"),
            "test_roc_auc": champ_diag.get("roc_auc"),
            "val_pr_auc": champ_diag.get("val_pr_auc"),
            "note": "the same refit computed inside the M4 champion run (docs/ml/RESULTS.md §9)",
        },
        "registry": None,
    }
    return ColdRun(results=_clean(results), candidate=cand, bundle=bundle, guards=[test_guard])


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


# ---------------------------------------------------------------- outputs
def write_outputs(run: ColdRun, out_dir: Path) -> dict[str, Path]:
    d = out_dir / ROLE
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    results = d / "results.json"
    results.write_text(json.dumps(run.results, indent=1))
    run.bundle.save(d / "risk_bundle")
    return {"results": results, "bundle": d / "risk_bundle"}


def promotion_check(results: dict[str, Any], current_champion_pr_auc: float | None) -> list[str]:
    """Reasons NOT to promote (empty list: promote)."""
    reasons = []
    if results["data"]["synthetic"] or results["data"]["source"] != DATA_SOURCE:
        reasons.append("trained on synthetic data")
    if results["leakage_audit"]["violations"] != 0:
        reasons.append("leakage audit did not pass")
    if set(results["model"]["features"]) & set(HISTORY_FEATURES):
        reasons.append("the feature list contains history features")
    test = results["test"]["final"]
    if not test["pr_auc"] > test["base_rate"]:
        reasons.append(
            f"test PR-AUC {test['pr_auc']:.4f} does not beat the base rate {test['base_rate']:.4f}"
        )
    if current_champion_pr_auc is not None and test["pr_auc"] < current_champion_pr_auc:
        reasons.append(
            f"test PR-AUC {test['pr_auc']:.4f} < current cold champion "
            f"{current_champion_pr_auc:.4f}"
        )
    return reasons


def register(
    run: ColdRun, paths: dict[str, Path], *, tracking_uri: str | None, promote: bool
) -> dict[str, Any]:
    """Log to MLflow, register ``release_risk_ci_cold`` (alias challenger), optionally promote."""
    import mlflow
    from mlflow import MlflowClient
    from mlflow.exceptions import MlflowException
    from mlflow.models import infer_signature

    from rl_ml import registry

    uri = registry.configure(tracking_uri)
    res = run.results
    if res["data"]["synthetic"]:
        raise ColdModelError("refusing to register a model trained on synthetic data")
    mlflow.set_experiment(COLD_EXPERIMENT)
    test = res["test"]["final"]
    with mlflow.start_run(run_name=f"{ROLE}-{res['model']['family']}") as active:
        mlflow.set_tags(
            {
                "role": ROLE,
                "data_source": res["data"]["source"],
                "synthetic": "false",
                "dataset_md5": str(res["data"]["dataset_md5"]),
                "features_run_id": str(res["data"]["features_run_id"]),
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "label_variant": res["variant"],
                "probability_exposed": str(res["test"]["probability_exposed"]).lower(),
            }
        )
        mlflow.log_params(
            {f"final.{k}": v for k, v in res["model"]["params"].items()}
            | {"n_features": res["model"]["n_features"], "seed": res["model"]["seed"]}
        )
        mlflow.log_metrics(
            {
                "val_pr_auc": float(res["validation"]["val_pr_auc"]),
                "test_pr_auc": float(test["pr_auc"]),
                "test_roc_auc": float(test["roc_auc"]),
                "test_base_rate": float(test["base_rate"]),
                "test_lift_at_top_10pct": float(test["lift_at_top_10pct"]),
                "test_probability_exposed": float(bool(res["test"]["probability_exposed"])),
            }
        )
        mlflow.log_artifact(str(paths["results"]))
        mlflow.log_artifacts(str(paths["bundle"]), artifact_path="risk_bundle")
        X = run.bundle.frame([{}]).head(1)
        info = mlflow.xgboost.log_model(
            run.candidate.model.estimator,
            name="model",
            signature=infer_signature(X, run.candidate.score(X)),
            model_format="ubj",
            metadata={"role": ROLE, "risk_bundle_artifact": "risk_bundle"},
        )
        run_id = active.info.run_id
    client = MlflowClient()
    mv = mlflow.register_model(info.model_uri, COLD_MODEL_NAME)
    version = str(mv.version)
    tags = {
        "role": ROLE,
        "run_id": run_id,
        "synthetic": "false",
        "leakage_audit": "passed" if res["leakage_audit"]["violations"] == 0 else "failed",
        "label_variant": res["variant"],
        "test_pr_auc": repr(float(test["pr_auc"])),
        "test_roc_auc": repr(float(test["roc_auc"])),
        "test_base_rate": repr(float(test["base_rate"])),
        "val_pr_auc": repr(float(res["validation"]["val_pr_auc"])),
        "bands_raw": json.dumps(res["bands"]["raw"]),
        "risk_bundle_uri": f"runs:/{run_id}/risk_bundle",
    }
    for k, v in tags.items():
        client.set_model_version_tag(COLD_MODEL_NAME, version, k, v)
    client.set_registered_model_alias(COLD_MODEL_NAME, registry.CHALLENGER, version)
    client.update_registered_model(
        COLD_MODEL_NAME,
        description=(
            "CI failure risk WITHOUT build-history features, for repositories with no finished "
            "builds in ReleaseLens yet (day one). Not an outage probability."
        ),
    )
    out: dict[str, Any] = {
        "tracking_uri": uri,
        "experiment": COLD_EXPERIMENT,
        "run_id": run_id,
        "registered_model": COLD_MODEL_NAME,
        "version": version,
        "alias": registry.CHALLENGER,
    }
    if promote:
        try:
            champ = client.get_model_version_by_alias(COLD_MODEL_NAME, registry.CHAMPION)
            champ_pr = float(champ.tags["test_pr_auc"]) if str(champ.version) != version else None
        except MlflowException:
            champ_pr = None
        reasons = promotion_check(res, champ_pr)
        out["promotion"] = {"promoted": not reasons, "reasons": reasons}
        if not reasons:
            client.set_registered_model_alias(COLD_MODEL_NAME, registry.CHAMPION, version)
            client.set_model_version_tag(COLD_MODEL_NAME, version, "promoted_to_champion", "true")
    return out


def render_markdown(res: dict[str, Any]) -> str:
    """docs/ml/COLD_START_MODEL.md from a real run's results."""
    from rl_ml.card import verdict

    def f(v: Any, nd: int = 3) -> str:
        return "n/a" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)

    t, v = res["test"]["final"], res["validation"]
    g = res["test"]["calibration"]["gate"]
    ref = res["m4_diagnostic_reference"]
    reg = res.get("registry") or {}
    b = res["bands"]
    lines = [
        "# Cold-start model — `release_risk_ci_cold`",
        "",
        "> Generated by `python -m rl_ml.cold` from `docs/ml/results/cold_start.json` "
        "(a real run). Do not edit by hand.",
        "",
        "Served by the API when a repository has **no finished builds** in ReleaseLens yet "
        "(`cold_start: true` in the assessment). Same family and hyper-parameters as the champion, "
        f"trained **without** the {len(res['model']['excluded_history_features'])} history "
        "features, because masking history on the champion inflates scores "
        "([RESULTS.md §9](RESULTS.md)).",
        "",
        f"- Registered: `{reg.get('registered_model', COLD_MODEL_NAME)}/{reg.get('version', '?')}`"
        f" (promotion: {json.dumps(reg.get('promotion'))}).",
        f"- Features: {res['model']['n_features']} (FEATURE_SCHEMA_VERSION "
        f"`{res['data']['feature_schema_version']}`); feature mart "
        f"`{res['data']['features_run_id']}`.",
        f"- Test set evaluated {res['test']['evaluations']} time(s), after the model was locked.",
        "",
        "| metric | validation | test |",
        "|---|---|---|",
        f"| PR-AUC | {f(v['val_pr_auc'])} | {f(t['pr_auc'])} |",
        f"| ROC-AUC | {f(v['val_roc_auc'])} | {f(t['roc_auc'])} |",
        f"| base rate | {f(v['final']['base_rate'])} | {f(t['base_rate'])} |",
        f"| PR-AUC / base rate | {f(v['final']['pr_auc_over_base_rate'], 2)} | "
        f"{f(t['pr_auc_over_base_rate'], 2)} |",
        f"| precision @ top 10% | {f(v['final']['precision_at_top_10pct'])} | "
        f"{f(t['precision_at_top_10pct'])} |",
        "",
        f"M4 diagnostic of the same refit (champion run): test PR-AUC {f(ref['test_pr_auc'])}, "
        f"ROC-AUC {f(ref['test_roc_auc'])}, validation PR-AUC {f(ref['val_pr_auc'])}.",
        "",
        "Calibration gate (ADR-0012, test): "
        f"ECE {f(g['ece_10_equal_mass'], 4)}, Brier skill {f(g['brier_skill_score'], 4)}, "
        f"slope {f(g['calibration_slope'])} → **{'PASS' if g['passed'] else 'FAIL'}** "
        f"(method `{res['test']['calibration']['selected_method']}`); probability exposed: "
        f"**{'yes' if res['test']['probability_exposed'] else 'no'}**.",
        "",
        "Band cuts (validation recall 0.80 / 0.50 / 0.20): raw "
        f"{f(b['raw']['MEDIUM'], 4)} / {f(b['raw']['HIGH'], 4)} / {f(b['raw']['CRITICAL'], 4)}, "
        f"risk score {b['risk_score']['MEDIUM']} / {b['risk_score']['HIGH']} / "
        f"{b['risk_score']['CRITICAL']}.",
        "",
        f"Discrimination without history is **{verdict(t['roc_auc'])}** (test ROC-AUC "
        f"{f(t['roc_auc'])}): the score reflects the change's size and the codebase, not the "
        "repository's track record. Treat day-one scores as a rough ranking; the champion takes "
        "over after the repository's first finished build in ReleaseLens.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    from rl_ml.train import repo_root

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m rl_ml.cold", description=__doc__)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--features-run-id")
    src.add_argument("--synthetic", action="store_true", help="SYNTHETIC data (smoke runs only)")
    p.add_argument("--champion-results", type=Path, default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", type=Path, default=repo_root() / "ml" / "artifacts" / "runs")
    p.add_argument("--docs-dir", type=Path, default=repo_root() / "docs" / "ml")
    p.add_argument("--no-docs", action="store_true")
    p.add_argument("--no-mlflow", action="store_true")
    p.add_argument("--promote", action="store_true", help="move alias champion if criteria hold")
    p.add_argument("--tracking-uri", default=None)
    p.add_argument(
        "--render-only",
        action="store_true",
        help="re-render COLD_START_MODEL.md from docs/ml/results/cold_start.json",
    )
    args = p.parse_args(argv)
    if args.render_only:
        res = json.loads((args.docs_dir / "results" / f"{ROLE}.json").read_text())
        (args.docs_dir / "COLD_START_MODEL.md").write_text(render_markdown(res))
        return 0

    if not (args.synthetic or args.features_run_id):
        p.error("one of --features-run-id or --synthetic is required")
    if args.synthetic:
        from rl_ml.data import prepare_mart
        from rl_ml.synthetic import SYNTHETIC_SOURCE, make_synthetic_mart
        from rl_ml.train import TrainConfig, run_training

        log.warning("SYNTHETIC data: metrics from this run are NOT real results")
        mart = prepare_mart(
            make_synthetic_mart(seed=args.seed),
            run_id=f"synthetic-seed{args.seed}",
            source=SYNTHETIC_SOURCE,
            dataset_md5=None,
        )
        champ = run_training(mart, TrainConfig(trials=3, seed=args.seed)).results
    else:
        from rl_ml.data import load_mart

        path = args.champion_results or args.docs_dir / "results" / "failed_or_errored.json"
        champ = json.loads(path.read_text())
        if champ["data"]["features_run_id"] != args.features_run_id:
            log.warning(
                "champion was trained on mart %s, cold model uses %s",
                champ["data"]["features_run_id"],
                args.features_run_id,
            )
        mart = load_mart(args.features_run_id)

    run = run_cold_training(mart, champ, seed=args.seed)
    paths = write_outputs(run, args.out_dir)
    if not args.no_mlflow:
        run.results["registry"] = register(
            run, paths, tracking_uri=args.tracking_uri, promote=args.promote
        )
        paths["results"].write_text(json.dumps(run.results, indent=1))
    if not args.no_docs and not mart.is_synthetic:
        (args.docs_dir / "results").mkdir(parents=True, exist_ok=True)
        (args.docs_dir / "results" / f"{ROLE}.json").write_text(json.dumps(run.results, indent=1))
        (args.docs_dir / "COLD_START_MODEL.md").write_text(render_markdown(run.results))
    r = run.results
    sys.stdout.write(
        json.dumps(
            {
                "val_pr_auc": r["validation"]["val_pr_auc"],
                "test_pr_auc": r["test"]["final"]["pr_auc"],
                "test_roc_auc": r["test"]["final"]["roc_auc"],
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
