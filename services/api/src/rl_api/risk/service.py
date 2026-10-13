"""Scoring one request and shaping stored assessments for the API (docs/api/RISK_ASSESSMENTS.md).

Scoring is pure CPU on an already-loaded bundle (no I/O): the router runs it in a worker thread.
"""

from __future__ import annotations

import math
from typing import Any, Final

from rl_api.risk.modelstore import LoadedModel
from rl_core.features.schema import FEATURE_SCHEMA_VERSION
from rl_db.models.risk import RiskAssessment

SHAP_UNIT: Final = "log-odds of the raw model score"


def score(model: LoadedModel, features: dict[str, float | None]) -> dict[str, Any]:
    """Assess one feature vector. Band cut-points, the score scale and the calibration gate all
    come from the bundle's metadata; nothing is hard-coded here."""
    row = {k: (math.nan if v is None else v) for k, v in features.items()}
    (result,) = model.bundle.assess([row], explain=True, top_k=3, full_contributions=True)
    return result


def shap_document(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit": SHAP_UNIT,
        "base_value": result.get("base_value"),
        "values": result.get("contributions"),
        "top_increasing": result["top_increasing"],
        "top_reducing": result["top_reducing"],
        "note": result.get("explanation_note"),
    }


def details_url(web_base_url: str, assessment_id: Any) -> str:
    return f"{web_base_url.rstrip('/')}/app/risk/{assessment_id}"


def assessment_summary(row: RiskAssessment, web_base_url: str) -> dict[str, Any]:
    """The contract's response body (also the list item)."""
    shap = row.shap or {}
    return {
        "assessment_id": row.id,
        "model_version": row.model_version,
        "feature_schema_version": row.feature_schema_version,
        "risk_score": row.risk_score,
        "risk_band": row.risk_band,
        "probability": row.probability,
        "top_increasing": shap.get("top_increasing") or [],
        "top_reducing": shap.get("top_reducing") or [],
        "history": {"finished_builds_used": row.history_used, "cold_start": row.cold_start},
        "details_url": details_url(web_base_url, row.id),
        "explanation_note": shap.get("note"),
        "repository": row.repository,
        "commit_sha": row.commit_sha,
        "event": row.event,
        "pull_request": row.pull_request,
        "triggered_at": row.triggered_at,
        "created_at": row.created_at,
    }


def assessment_detail(row: RiskAssessment, web_base_url: str) -> dict[str, Any]:
    shap = row.shap or {}
    return assessment_summary(row, web_base_url) | {
        "model_name": row.model_name,
        "raw_score": row.raw_score,
        "branch": row.branch,
        "ci_run_url": row.ci_run_url,
        "previous_built_sha": row.previous_built_sha,
        "extractor": row.extractor,
        "features": row.features,
        "change_facts": row.change_facts,
        "shap": {
            "unit": shap.get("unit", SHAP_UNIT),
            "base_value": shap.get("base_value"),
            "values": shap.get("values"),
        },
    }


def row_values(
    *,
    model: LoadedModel,
    result: dict[str, Any],
    features: dict[str, float | None],
    change_facts: dict[str, Any],
    history_used: int,
    cold_start: bool,
) -> dict[str, Any]:
    return {
        "features": features,
        "change_facts": change_facts,
        "model_name": model.name,
        "model_version": model.version,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "raw_score": float(result["raw_score"]),
        "risk_score": int(result["risk_score"]),
        "risk_band": str(result["risk_band"]),
        "probability": result["probability"],
        "shap": shap_document(result),
        "history_used": history_used,
        "cold_start": cold_start,
    }
