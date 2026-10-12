"""MLflow tracking + model registry (spec section 8.9).

Every training run logs params, metrics, artifacts (results JSON, figures, the serving bundle),
the dataset version (TravisTorrent md5 + feature mart run_id), ``FEATURE_SCHEMA_VERSION``, the
git SHA and a model signature. The best model is registered as ``release_risk_ci`` with alias
``challenger``. Promotion to ``champion`` (:func:`promote`, CLI below) requires:

1. test PR-AUC >= the current champion's (or no champion), same label variant;
2. the leakage tests pass (``ml/tests/test_leakage.py`` is run) and the version was trained on a
   mart that passed the in-pipeline leakage audit;
3. a generated model card ``docs/ml/MODEL_CARD.md`` that names this exact model version.

CLI::

    python -m rl_ml.registry promote --version 3 [--tracking-uri http://127.0.0.1:25000]
    python -m rl_ml.registry show
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from rl_core.features.schema import FEATURE_SCHEMA_VERSION

if TYPE_CHECKING:
    from rl_ml.train import TrainingRun

log = logging.getLogger("rl_ml.registry")

MODEL_NAME: Final = "release_risk_ci"
EXPERIMENT: Final = "release_risk_ci"
CHALLENGER: Final = "challenger"
CHAMPION: Final = "champion"
DEFAULT_TRACKING_URI: Final = "http://127.0.0.1:25000"


class PromotionRefusedError(RuntimeError):
    def __init__(self, reasons: list[str], decision: dict[str, Any]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.decision = decision


def configure(tracking_uri: str | None = None) -> str:
    import mlflow

    uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI") or DEFAULT_TRACKING_URI
    mlflow.set_tracking_uri(uri)
    return uri


def _flatten(prefix: str, d: dict[str, Any], out: dict[str, float]) -> None:
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, bool) or (isinstance(v, int | float) and v is not None):
            out[key] = float(v)
        elif isinstance(v, dict) and k not in {"confusion_matrix"}:
            _flatten(f"{key}.", v, out)


def run_metrics(results: dict[str, Any]) -> dict[str, float]:
    """The scalar metrics worth charting in MLflow (validation, test, cross-project, cold start)."""
    m: dict[str, float] = {}
    test = results["test"]
    for k in ("pr_auc", "roc_auc", "base_rate", "precision_at_top_10pct", "lift_at_top_10pct"):
        if test["final"].get(k) is not None:
            m[f"test_{k}"] = float(test["final"][k])
    for k in ("precision", "recall", "f1", "flagged_share"):
        v = test["final"]["at_threshold"].get(k)
        if v is not None:
            m[f"test_{k}_at_threshold"] = float(v)
    ci = test["final"].get("pr_auc_ci95_project_bootstrap") or {}
    if ci.get("low") is not None:
        m["test_pr_auc_ci_low"], m["test_pr_auc_ci_high"] = float(ci["low"]), float(ci["high"])
    for k in ("pr_auc", "roc_auc"):
        if test["cold_start"].get(k) is not None:
            m[f"test_cold_start_{k}"] = float(test["cold_start"][k])
    g = test["calibration"]["gate"]
    for k in (
        "ece_10_equal_mass",
        "brier",
        "brier_skill_score",
        "calibration_slope",
        "calibration_intercept",
    ):
        if g.get(k) is not None:
            m[f"test_cal_{k}"] = float(g[k])
    m["test_probability_exposed"] = float(bool(test["probability_exposed"]))
    pp = test["per_project"]["summary"]
    if pp.get("pr_auc_median") is not None:
        m["test_per_project_pr_auc_median"] = float(pp["pr_auc_median"])
    val = results["validation"]["final"]
    for k in ("pr_auc", "roc_auc", "base_rate"):
        if val.get(k) is not None:
            m[f"val_{k}"] = float(val[k])
    for key, res in results["cross_project"]["results"].items():
        if res.get("pr_auc") is not None:
            m[f"xproj_{key}_pr_auc"] = float(res["pr_auc"])
            m[f"xproj_{key}_base_rate"] = float(res["base_rate"])
    for band, r in test["bands"]["at_or_above"].items():
        for k in ("precision", "recall", "flagged_share"):
            if r.get(k) is not None:
                m[f"test_band_{band.lower()}_{k}"] = float(r[k])
    return m


def _tags(results: dict[str, Any]) -> dict[str, str]:
    d = results["data"]
    return {
        "data_source": str(d["source"]),
        "synthetic": str(d["synthetic"]).lower(),
        "dataset_md5": str(d["dataset_md5"]),
        "features_run_id": str(d["features_run_id"]),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_variant": results["variant"],
        "git_sha": str(results["git"]["sha"]),
        "git_dirty": str(results["git"]["dirty"]).lower(),
        "final_model": results["selection"]["final_model"],
        "test_set_evaluations": str(results["test"]["evaluations"]),
        "leakage_audit": "passed" if results["leakage_audit"]["violations"] == 0 else "failed",
        "probability_exposed": str(results["test"]["probability_exposed"]).lower(),
    }


def log_training_run(
    run: TrainingRun,
    paths: dict[str, Path],
    *,
    tracking_uri: str | None = None,
    register: bool = True,
) -> dict[str, Any]:
    """Log one training run (parent + one nested run per candidate); optionally register it."""
    import mlflow
    from mlflow.data.dataset_source_registry import resolve_dataset_source
    from mlflow.data.meta_dataset import MetaDataset
    from mlflow.models import infer_signature

    from rl_ml.models import SklearnModel, XGBModel

    uri = configure(tracking_uri)
    res = run.results
    synthetic = bool(res["data"]["synthetic"])
    experiment = f"{EXPERIMENT}_synthetic" if synthetic else EXPERIMENT
    mlflow.set_experiment(experiment)
    tags = _tags(res)
    run_name = f"{res['variant']}-{res['selection']['final_model']}"
    out: dict[str, Any] = {"tracking_uri": uri, "experiment": experiment}
    if synthetic and register:
        log.warning("synthetic run: not registering a model version")
        register = False

    with mlflow.start_run(run_name=run_name) as parent:
        mlflow.set_tags(tags)
        cfg = res["config"]
        mlflow.log_params(
            {
                "trials_per_xgb_search": cfg["trials"],
                "seed": cfg["seed"],
                "label_variant": res["variant"],
                "holdout_fraction": cfg["holdout_fraction"],
                "final_model": res["selection"]["final_model"],
                "n_features": len(res["selection"]["features"]),
                "time_features_used": res["time_features"].get("time_features_used", False),
                "decision_threshold": res["test"]["final"]["at_threshold"]["threshold"],
                "calibration_method": res["test"]["calibration"]["selected_method"],
                "band_cut_medium": res["bands"]["raw"]["MEDIUM"],
                "band_cut_high": res["bands"]["raw"]["HIGH"],
                "band_cut_critical": res["bands"]["raw"]["CRITICAL"],
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "dataset_md5": res["data"]["dataset_md5"],
                "features_run_id": res["data"]["features_run_id"],
            }
            | {f"final.{k}": v for k, v in run.final.params.items()}
        )
        mlflow.log_metrics(run_metrics(res))
        source = (
            f"s3://releaselens-data/features/release_features/schema={FEATURE_SCHEMA_VERSION}/"
            f"run_id={res['data']['features_run_id']}/"
        )
        try:
            ds = MetaDataset(  # type: ignore[abstract]
                resolve_dataset_source(source),
                name=f"release_features@{res['data']['features_run_id']}",
                digest=str(res["data"]["dataset_md5"])[:36],
            )
            mlflow.log_input(ds, context="training")
        except Exception as exc:  # dataset lineage is best-effort; tags carry the same facts
            log.warning("could not log dataset input: %s", exc)

        for c in run.outcome.candidates:
            row = next(r for r in res["candidates"] if r["name"] == c.name)
            with mlflow.start_run(run_name=c.name, nested=True):
                mlflow.set_tags(tags | {"candidate": c.name})
                mlflow.log_params(dict(c.params.items()) | {"n_features": len(c.features)})
                mlflow.log_metrics(
                    {"val_pr_auc": c.val_pr_auc}
                    | (
                        {"val_roc_auc": row["val_roc_auc"]}
                        if row["val_roc_auc"] is not None
                        else {}
                    )
                    | {"fit_seconds": c.fit_seconds}
                )
                for i, trial in enumerate(c.search_history):
                    mlflow.log_metric("search_val_pr_auc", float(trial["val_pr_auc"]), step=i)
                ref = res["test"]["reference_candidates"].get(c.name)
                if ref and ref.get("pr_auc") is not None:
                    mlflow.log_metric("test_pr_auc_reference_only", float(ref["pr_auc"]))

        mlflow.log_artifact(str(paths["results"]))
        for key in ("pr_curve", "reliability", "shap"):
            if key in paths:
                mlflow.log_artifact(str(paths[key]), artifact_path="figures")
        mlflow.log_artifacts(str(paths["bundle"]), artifact_path="risk_bundle")

        X = run.X_example
        scores = run.final.score(X)
        signature = infer_signature(X, scores)
        metadata = {
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "label_variant": res["variant"],
            "bands_raw": res["bands"]["raw"],
            "bands_risk_score": res["bands"].get("risk_score"),
            "probability_exposed": res["test"]["probability_exposed"],
            "calibration_method": res["test"]["calibration"]["selected_method"],
            "risk_bundle_artifact": "risk_bundle",
            "dataset_md5": res["data"]["dataset_md5"],
            "features_run_id": res["data"]["features_run_id"],
        }
        if isinstance(run.final.model, XGBModel):
            info = mlflow.xgboost.log_model(
                run.final.model.estimator,
                name="model",
                signature=signature,
                input_example=X.head(3),
                model_format="ubj",
                metadata=metadata,
            )
        elif isinstance(run.final.model, SklearnModel):
            info = mlflow.sklearn.log_model(
                run.final.model.estimator,
                name="model",
                signature=signature,
                input_example=X.head(3),
                metadata=metadata,
                skops_trusted_types=["numpy.dtype"],
            )
        else:
            info = None
            log.warning(
                "final model %s is a baseline; logged as bundle artifacts only", run.final.name
            )
        out["run_id"] = parent.info.run_id
        out["model_uri"] = info.model_uri if info else None

    if register and info is not None:
        out |= register_model(info.model_uri, res, run_id=out["run_id"])
    return out


def register_model(model_uri: str, results: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    import mlflow
    from mlflow import MlflowClient

    client = MlflowClient()
    mv = mlflow.register_model(model_uri, MODEL_NAME)
    version = str(mv.version)
    test = results["test"]
    vtags = _tags(results) | {
        "run_id": run_id,
        "test_pr_auc": repr(float(test["final"]["pr_auc"])),
        "test_roc_auc": repr(float(test["final"]["roc_auc"])),
        "test_base_rate": repr(float(test["final"]["base_rate"])),
        "val_pr_auc": repr(float(results["selection"]["val_pr_auc"])),
        "bands_raw": json.dumps(results["bands"]["raw"]),
        "risk_bundle_uri": f"runs:/{run_id}/risk_bundle",
    }
    for k, v in vtags.items():
        client.set_model_version_tag(MODEL_NAME, version, k, v)
    client.set_registered_model_alias(MODEL_NAME, CHALLENGER, version)
    client.update_registered_model(
        MODEL_NAME,
        description=(
            "Build/CI failure risk from historical patterns (TravisTorrent 2011-2015 Java builds). "
            "Not an outage probability. Serve risk_score/bands from the risk_bundle artifact."
        ),
    )
    return {"registered_model": MODEL_NAME, "version": version, "alias": CHALLENGER}


# ---------------------------------------------------------------- promotion
def run_leakage_tests() -> tuple[bool, str]:
    from rl_ml.train import repo_root

    test_file = repo_root() / "ml" / "tests" / "test_leakage.py"
    proc = subprocess.run(  # noqa: S603 - our interpreter + a fixed test path
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root(),
    )
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-3:])
    return proc.returncode == 0, tail


def promote(
    version: str | int,
    *,
    client: Any = None,
    card_path: Path | None = None,
    tracking_uri: str | None = None,
    leakage_check: Any = run_leakage_tests,
) -> dict[str, Any]:
    """Move the ``champion`` alias to ``version`` if every promotion criterion holds."""
    from mlflow.exceptions import MlflowException

    from rl_ml.train import repo_root

    if client is None:
        from mlflow import MlflowClient

        configure(tracking_uri)
        client = MlflowClient()
    v = str(version)
    mv = client.get_model_version(MODEL_NAME, v)
    tags = dict(mv.tags)
    reasons: list[str] = []
    decision: dict[str, Any] = {"model": MODEL_NAME, "version": v}

    challenger_pr = float(tags["test_pr_auc"])
    decision["challenger_test_pr_auc"] = challenger_pr
    try:
        champ = client.get_model_version_by_alias(MODEL_NAME, CHAMPION)
    except MlflowException:
        champ = None
    if champ is not None and str(champ.version) != v:
        ct = dict(champ.tags)
        decision["champion_version"] = str(champ.version)
        decision["champion_test_pr_auc"] = float(ct["test_pr_auc"])
        if ct.get("label_variant") != tags.get("label_variant"):
            reasons.append(
                f"label variant differs from the champion ({tags.get('label_variant')} vs "
                f"{ct.get('label_variant')}); "
                "test PR-AUCs are not comparable"
            )
        elif challenger_pr < float(ct["test_pr_auc"]):
            reasons.append(
                f"test PR-AUC {challenger_pr:.4f} < champion {float(ct['test_pr_auc']):.4f}"
            )
    else:
        decision["champion_version"] = None

    if tags.get("synthetic") != "false":
        reasons.append("model was trained on synthetic data")
    if tags.get("leakage_audit") != "passed":
        reasons.append("training-time leakage audit did not pass")
    ok, tail = leakage_check()
    decision["leakage_tests"] = {"passed": ok, "summary": tail}
    if not ok:
        reasons.append("ml/tests/test_leakage.py failed")

    card = card_path or repo_root() / "docs" / "ml" / "MODEL_CARD.md"
    marker = f"{MODEL_NAME}/{v}"
    card_ok = card.is_file() and marker in card.read_text()
    decision["model_card"] = {"path": str(card), "names_version": card_ok}
    if not card_ok:
        reasons.append(f"{card} missing or does not name {marker} (run python -m rl_ml.card)")

    if reasons:
        decision["promoted"] = False
        decision["reasons"] = reasons
        raise PromotionRefusedError(reasons, decision)
    client.set_registered_model_alias(MODEL_NAME, CHAMPION, v)
    client.set_model_version_tag(MODEL_NAME, v, "promoted_to_champion", "true")
    decision["promoted"] = True
    return decision


def show(tracking_uri: str | None = None) -> dict[str, Any]:
    from mlflow import MlflowClient

    configure(tracking_uri)
    client = MlflowClient()
    versions = client.search_model_versions(f"name='{MODEL_NAME}'")
    rm = client.get_registered_model(MODEL_NAME)
    return {
        "aliases": dict(rm.aliases),
        "versions": [
            {
                "version": mv.version,
                "run_id": mv.run_id,
                "tags": {
                    k: mv.tags[k]
                    for k in sorted(mv.tags)
                    if k.startswith(("test_", "label", "final", "prob"))
                },
            }
            for mv in sorted(versions, key=lambda m: int(m.version))
        ],
    }


def download_bundle(
    alias: str = CHAMPION, *, tracking_uri: str | None = None, dst: Path | None = None
) -> Path:
    """Fetch the serving bundle of the model version behind ``alias`` (for the API)."""
    import mlflow
    from mlflow import MlflowClient

    configure(tracking_uri)
    mv = MlflowClient().get_model_version_by_alias(MODEL_NAME, alias)
    target = dst or Path(tempfile.mkdtemp(prefix="rl_bundle_"))
    path = mlflow.artifacts.download_artifacts(
        run_id=mv.run_id, artifact_path="risk_bundle", dst_path=str(target)
    )
    return Path(path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m rl_ml.registry")
    p.add_argument("--tracking-uri", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("promote", help="promote a model version to champion (checks criteria)")
    pr.add_argument("--version", required=True)
    pr.add_argument("--card", type=Path, default=None)
    sub.add_parser("show", help="aliases and versions")
    args = p.parse_args(argv)
    if args.cmd == "show":
        sys.stdout.write(json.dumps(show(args.tracking_uri), indent=1) + "\n")
        return 0
    try:
        decision = promote(args.version, card_path=args.card, tracking_uri=args.tracking_uri)
    except PromotionRefusedError as exc:
        sys.stdout.write(json.dumps(exc.decision, indent=1) + "\n")
        log.error("promotion refused: %s", exc)
        return 1
    sys.stdout.write(json.dumps(decision, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
