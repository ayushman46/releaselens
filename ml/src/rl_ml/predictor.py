"""The serving bundle: everything the API needs to turn a feature row into an assessment.

A bundle directory holds the model (XGBoost UBJSON, or joblib for the sklearn families) and
JSON metadata: feature list + schema version, the validation score quantile table
(``risk_score``), band cut-points, the calibrator **only if the ADR-0012 gate passed**, training
distribution statistics for explanation text, and PSI training bins. No numbers are hard-coded
on the serving side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from rl_core.features.schema import FEATURE_SCHEMA_VERSION
from rl_ml.bands import BandCuts, ScoreScale
from rl_ml.calibrate import Calibrator
from rl_ml.explain import DISCLAIMER, BackgroundStats, contributions, explain_rows
from rl_ml.monitoring import FeatureBins

BUNDLE_FORMAT: Final = 1


class BundleError(RuntimeError):
    pass


@dataclass
class RiskModelBundle:
    model: Any
    features: tuple[str, ...]
    scale: ScoreScale
    cuts: BandCuts
    calibrator: Calibrator | None
    calibration: dict[str, Any]
    stats: BackgroundStats
    psi_bins: dict[str, FeatureBins]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def probability_exposed(self) -> bool:
        return self.calibrator is not None and bool(self.calibration.get("passed"))

    # ------------------------------------------------------------ inference
    def frame(self, rows: list[dict[str, Any]] | pd.DataFrame) -> pd.DataFrame:
        if not isinstance(rows, pd.DataFrame):
            # Serving path (one row per request): build the matrix directly; the generic pandas
            # path below costs ~10 ms per call. Same semantics: missing or non-numeric → NaN.
            arr = np.array([[_number(r.get(f)) for f in self.features] for r in rows], dtype=float)
            return pd.DataFrame(arr.reshape(len(rows), len(self.features)), columns=self.features)
        df = rows.copy()
        missing = [f for f in self.features if f not in df.columns]
        for f in missing:  # unknown → NaN, never 0 (docs/api/RISK_ASSESSMENTS.md)
            df[f] = np.nan
        return (
            df.loc[:, list(self.features)].apply(pd.to_numeric, errors="coerce").astype(np.float64)
        )

    def _raw_and_contributions(
        self, X: pd.DataFrame, explain: bool
    ) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
        """Raw scores (+ SHAP contributions and base values). XGBoost goes straight to the
        booster on a NumPy matrix: identical numbers to ``model.score`` / ``contributions``
        (a test asserts it) without the sklearn wrapper's per-call validation."""
        from rl_ml.models import XGBModel

        if isinstance(self.model, XGBModel):
            import xgboost as xgb

            booster = self.model.booster
            dm = xgb.DMatrix(
                X.to_numpy(dtype=np.float64), feature_names=list(self.features), nthread=1
            )
            raw = np.asarray(booster.predict(dm), dtype=np.float64)
            if not explain:
                return raw, None, None
            c = booster.predict(dm, pred_contribs=True)
            return raw, np.asarray(c[:, :-1], np.float64), np.asarray(c[:, -1], np.float64)
        raw = np.asarray(self.model.score(X), dtype=np.float64)
        if not explain:
            return raw, None, None
        try:
            contrib, base = contributions(self.model, X)
        except TypeError:  # baselines have no attribution
            return raw, None, None
        return raw, contrib, base

    def assess(
        self,
        rows: list[dict[str, Any]] | pd.DataFrame,
        *,
        explain: bool = True,
        top_k: int = 3,
        full_contributions: bool = False,
    ) -> list[dict[str, Any]]:
        """Score rows. ``full_contributions`` adds the whole SHAP vector (``contributions``:
        feature → log-odds contribution) and ``base_value`` to each result."""
        X = self.frame(rows)
        raw, contrib, base = self._raw_and_contributions(X, explain)
        scores = self.scale.risk_score(raw)
        bands = self.cuts.band(raw)
        prob = (
            self.calibrator.predict(raw) if self.probability_exposed and self.calibrator else None
        )
        expl: list[dict[str, Any]] = [{"top_increasing": [], "top_reducing": []}] * len(X)
        if contrib is not None:
            expl = explain_rows(contrib, X, self.stats, top_k)
        out = []
        for i in range(len(X)):
            item: dict[str, Any] = {
                "raw_score": float(raw[i]),
                "risk_score": int(scores[i]),
                "risk_band": str(bands[i]),
                "probability": float(prob[i]) if prob is not None else None,
                "top_increasing": expl[i]["top_increasing"],
                "top_reducing": expl[i]["top_reducing"],
                "explanation_note": DISCLAIMER,
            }
            if full_contributions:
                item["contributions"] = (
                    {f: float(contrib[i, j]) for j, f in enumerate(self.features)}
                    if contrib is not None
                    else None
                )
                item["base_value"] = float(base[i]) if base is not None else None
            out.append(item)
        return out

    # ------------------------------------------------------------ persistence
    def save(self, directory: str | Path) -> list[Path]:
        from rl_ml.models import XGBModel

        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        if isinstance(self.model, XGBModel):
            model_file = d / "model.ubj"
            self.model.estimator.save_model(model_file)
            kind = "xgboost"
        else:
            import joblib

            model_file = d / "model.joblib"
            joblib.dump(self.model, model_file)
            kind = "joblib"
        metadata = {
            "bundle_format": BUNDLE_FORMAT,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "model_kind": kind,
            "model_file": model_file.name,
            "features": list(self.features),
            "score_scale": {"validation_quantiles": list(self.scale.quantiles)},
            "bands": self.cuts.as_dict(self.scale),
            "probability_exposed": self.probability_exposed,
            "calibrator": self.calibrator.to_dict()
            if self.probability_exposed and self.calibrator
            else None,
            "calibration": {k: v for k, v in self.calibration.items() if k != "reliability"},
            "meta": self.meta,
        }
        files = [model_file, d / "metadata.json", d / "background_stats.json", d / "psi_bins.json"]
        files[1].write_text(json.dumps(metadata, indent=1, sort_keys=True, default=_json_default))
        files[2].write_text(json.dumps(self.stats.to_dict(), sort_keys=True))
        files[3].write_text(
            json.dumps({k: b.to_dict() for k, b in self.psi_bins.items()}, sort_keys=True)
        )
        return files

    @staticmethod
    def load(directory: str | Path) -> RiskModelBundle:
        d = Path(directory)
        meta = json.loads((d / "metadata.json").read_text())
        if meta.get("bundle_format") != BUNDLE_FORMAT:
            raise BundleError(f"unsupported bundle format {meta.get('bundle_format')}")
        if meta["feature_schema_version"] != FEATURE_SCHEMA_VERSION:
            raise BundleError(
                f"bundle feature schema {meta['feature_schema_version']} != "
                f"rl_core {FEATURE_SCHEMA_VERSION}"
            )
        if meta["model_kind"] == "xgboost":
            import xgboost as xgb

            from rl_ml.models import XGBModel

            est = xgb.XGBClassifier()
            est.load_model(d / meta["model_file"])
            model: Any = XGBModel(est)
        else:
            import joblib

            model = joblib.load(d / meta["model_file"])  # trusted: written by our own registry
        cal = meta.get("calibrator")
        return RiskModelBundle(
            model=model,
            features=tuple(meta["features"]),
            scale=ScoreScale(tuple(meta["score_scale"]["validation_quantiles"])),
            cuts=BandCuts.from_dict(meta["bands"]),
            calibrator=Calibrator.from_dict(cal) if cal else None,
            calibration=meta.get("calibration", {}),
            stats=BackgroundStats.from_dict(json.loads((d / "background_stats.json").read_text())),
            psi_bins={
                k: FeatureBins.from_dict(v)
                for k, v in json.loads((d / "psi_bins.json").read_text()).items()
            },
            meta=meta.get("meta", {}),
        )


def _number(value: Any) -> float:
    """``pd.to_numeric(value, errors="coerce")`` for one scalar: numbers, booleans and numeric
    strings convert; ``None`` and anything else is NaN."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _json_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")
