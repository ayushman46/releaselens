"""Assemble, serialise and publish the analytics report (``findings.json`` + ``FINDINGS.md``).

Determinism: the JSON carries no wall-clock time and no host paths outside the input root;
DuckDB only counts, every float is computed in Python from fixed-order inputs and rounded to 12
significant digits, and the bootstrap is seeded. Re-running on the same inputs gives the same
bytes (``tests/test_report.py``). The date belongs to ``FINDINGS.md`` only.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

from rl_analysis import __version__
from rl_analysis.effects import holm
from rl_analysis.findings import CAUSATION, LARGE_N, Analysis, AnalysisConfig
from rl_analysis.inputs import RunInputs, check_naive_link, connect, load
from rl_analysis.settings import AnalysisSettings
from rl_core.features.schema import FEATURE_SCHEMA_VERSION

REPORT_SCHEMA_VERSION: Final = "1.0.0"
REPORT_NAME: Final = "releaselens-historical-ci-analytics"
FINDING_ORDER: Final = (
    "change_size",
    "src_test_churn",
    "previous_build",
    "duration",
    "projects",
    "monthly_trend",
    "core_team",
    "pull_requests",
)
_LIBRARIES: Final = ("duckdb", "numpy", "scipy", "statsmodels", "polars")
_DATASET_SOURCES: Final = {
    "travistorrent-5-3-2016": {
        "subset": "gh_lang = 'java', one row per tr_build_id (ADR-0017)",
        "source": "TravisTorrent travistorrent_5_3_2016 (Figshare 10.6084/m9.figshare.19314170, "
        "CC BY 4.0; Beller, Gousios & Zaidman, MSR 2017)",
    },
}


@dataclass(frozen=True, slots=True)
class ReportRequest:
    run_id: str
    input_root: str
    duckdb_path: Path | None = None
    config: AnalysisConfig = field(default_factory=AnalysisConfig)
    command: str = ""


def _clean(value: Any) -> Any:
    """JSON-safe, platform-stable values: floats to 12 significant digits, NaN/inf -> None."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(f"{value:.12g}")
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item())
    raise TypeError(f"cannot serialise {type(value).__name__}")


def dumps(report: Mapping[str, Any]) -> bytes:
    """Canonical bytes of a report (UTF-8, 2-space indent, trailing newline)."""
    text = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def build_report(request: ReportRequest, settings: AnalysisSettings) -> dict[str, Any]:
    """Load the run, run every analysis and return the (cleaned) report dict."""
    inputs = RunInputs.create(request.input_root, request.run_id, FEATURE_SCHEMA_VERSION)
    con = connect(settings, s3=inputs.is_s3)
    try:
        provenance = load(con, inputs)
        analysis = Analysis(con, request.config)
        checks: dict[str, Any] = {"cross_checks": analysis.checks()}
        if request.duckdb_path is not None:
            naive_check = check_naive_link(con, request.duckdb_path)
            if naive_check["status"] != "ok":
                raise RuntimeError(
                    f"naive-link mirror differs from int_build_history: {naive_check}"
                )
            checks["naive_link_vs_int_build_history"] = naive_check
        else:
            checks["naive_link_vs_int_build_history"] = {
                "status": "not_run",
                "reason": "no --duckdb-path given",
            }
        population = analysis.population()
        findings = {
            "change_size": analysis.finding_change_size(),
            "src_test_churn": analysis.finding_src_test_churn(),
            "previous_build": analysis.finding_previous_build(),
            "duration": analysis.finding_duration(),
            "projects": analysis.finding_projects(),
            "monthly_trend": analysis.finding_monthly_trend(),
            "core_team": analysis.finding_core_team(),
            "pull_requests": analysis.finding_pull_requests(),
        }
        tests = analysis.tests
        stratified = analysis.stratified
    finally:
        con.close()

    ok = [t for t in tests if t.get("status") == "ok"]
    for t, p in zip(ok, holm([t["p_value"] for t in ok]), strict=True):
        t["p_value_holm"] = p
    ok_mh = [t for t in stratified if t.get("status") == "ok"]
    for t, p in zip(ok_mh, holm([t["p_value"] for t in ok_mh]), strict=True):
        t["p_value_holm"] = p
    by_key = {t["key"]: t for t in tests}
    if len(by_key) != len(tests):
        raise RuntimeError("duplicate chi-square test keys")
    for finding in findings.values():
        key = finding["headline"]["test_key"]
        if key is not None:
            finding["headline"]["p_value_holm"] = by_key[key].get("p_value_holm")

    manifest = provenance["feature_manifest"]
    report = {
        "report": REPORT_NAME,
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": request.run_id,
        "generated_by": request.command,
        "dataset": {
            "key": manifest["dataset"],
            "version": manifest["dataset_version"],
            **_DATASET_SOURCES.get(
                str(manifest["dataset"]),
                {"subset": "n/a", "source": "dataset not registered with rl_analysis"},
            ),
            "first_started_at": population["first_started_at"],
            "last_started_at": population["last_started_at"],
        },
        "inputs": provenance,
        "environment": {
            "rl_analysis": __version__,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            **{lib: version(lib) for lib in _LIBRARIES},
        },
        "config": request.config.as_dict(),
        "checks": checks,
        "population": population,
        "findings": [findings[k] for k in FINDING_ORDER],
        "chi_square_tests": {
            "family": "every chi-square test in this report",
            "adjustment": "holm (statsmodels.stats.multitest.multipletests)",
            "computed": len(ok),
            "not_computable": len(tests) - len(ok),
            "tests": tests,
        },
        "stratified_tests": {
            "family": "every within-project (Cochran-Mantel-Haenszel) test in this report",
            "adjustment": "holm (statsmodels.stats.multitest.multipletests)",
            "computed": len(ok_mh),
            "not_computable": len(stratified) - len(ok_mh),
            "tests": stratified,
        },
        "caveats": [
            CAUSATION,
            LARGE_N,
            "Label = failed or errored (ADR-0005): errored builds often break in setup or "
            "infrastructure rather than in the tests.",
            "TravisTorrent Java projects 2011-2015 (Travis CI era); not evidence about any "
            "particular team's CI today.",
        ],
    }
    cleaned = _clean(report)
    if not isinstance(cleaned, dict):  # pragma: no cover - _clean preserves mappings
        raise TypeError("report must be a mapping")
    return cleaned


def write_outputs(report: Mapping[str, Any], out_dir: Path, *, generated_on: str) -> dict[str, Any]:
    """Write ``findings.json`` and ``FINDINGS.md`` into ``out_dir``; return paths + sha256."""
    from rl_analysis.markdown import render_markdown

    out_dir.mkdir(parents=True, exist_ok=True)
    body = dumps(report)
    json_path = out_dir / "findings.json"
    md_path = out_dir / "FINDINGS.md"
    json_path.write_bytes(body)
    md_path.write_text(render_markdown(report, generated_on=generated_on), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "md_path": str(md_path),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }
