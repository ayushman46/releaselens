"""Outputs, job summary and the fail-on-band gate (render_result.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import render_result as rr

RESPONSE: dict[str, Any] = {
    "assessment_id": "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
    "model_version": "release_risk_ci/7",
    "feature_schema_version": "1.0.0",
    "risk_score": 78,
    "risk_band": "HIGH",
    "probability": None,
    "top_increasing": [
        {
            "feature": "log1p_src_churn",
            "label": "Source lines changed | big",
            "value": 120,
            "contribution": 0.41,
            "text": "top 10%",
        }
    ],
    "top_reducing": [],
    "history": {"finished_builds_used": 3, "cold_start": True},
    "details_url": "https://app.releaselens.dev/app/risk/0192",
}
REQUEST = {"change_facts": {"src_churn": 120, "sloc": None, "is_pr": True}}
DIAG = {"unavailable": {"sloc": "work cap exceeded"}}


def evaluate(**kw: Any) -> rr.Outcome:
    args: dict[str, Any] = {
        "stage": "requested",
        "message": "",
        "http_status": "201",
        "response": RESPONSE,
        "request": REQUEST,
        "diagnostics": DIAG,
        "fail_on_band": "none",
    }
    args.update(kw)
    return rr.evaluate(**args)


def test_scored_response_sets_outputs_and_summary() -> None:
    out = evaluate()
    assert out.status == "ok"
    assert out.exit_code == 0
    assert out.outputs == {
        "risk-score": "78",
        "risk-band": "HIGH",
        "assessment-id": RESPONSE["assessment_id"],
        "details-url": RESPONSE["details_url"],
        "status": "ok",
    }
    s = out.summary
    assert "| Risk score | **78** / 100" in s
    assert "| Risk band | **HIGH** |" in s
    assert "| Top signals raising risk | Source lines changed \\| big (120) |" in s
    assert "| Top signals lowering risk | none |" in s
    assert f"> {rr.DISCLAIMER}" in s
    assert "not a production outage probability" in rr.DISCLAIMER
    assert "cold start (3 finished builds used)" in s
    assert "| Source lines of code (repository) | unknown (work cap exceeded) |" in s
    assert "Calibrated" not in s  # probability is null: never invented


def test_gate_compares_bands() -> None:
    assert evaluate(fail_on_band="HIGH").exit_code == 1
    assert evaluate(fail_on_band="high").exit_code == 1
    assert evaluate(fail_on_band="CRITICAL").exit_code == 0
    critical = {**RESPONSE, "risk_band": "CRITICAL"}
    assert evaluate(fail_on_band="CRITICAL", response=critical).exit_code == 1
    low = {**RESPONSE, "risk_band": "LOW"}
    assert evaluate(fail_on_band="HIGH", response=low).exit_code == 0
    assert evaluate(fail_on_band="MEDIUM").exit_code == 1  # not an accepted gate value


def test_503_is_a_notice_unless_gated() -> None:
    body = {"error": {"code": "model_unavailable", "message": "no champion model"}}
    out = evaluate(http_status="503", response=body)
    assert (out.status, out.exit_code) == ("unavailable", 0)
    assert out.annotations[0].startswith("::notice title=ReleaseLens::")
    assert "no model loaded" in out.annotations[0]
    assert out.outputs["risk-score"] == ""

    assert "not affected" in out.annotations[0]

    gated = evaluate(http_status="503", response=body, fail_on_band="HIGH")
    assert gated.exit_code == 1
    assert "not affected" not in gated.annotations[0]
    assert gated.annotations[-1].startswith("::error ")


def test_api_errors_do_not_fail_the_build_without_a_gate() -> None:
    body = {"error": {"code": "forbidden", "message": "missing scope"}}
    out = evaluate(http_status="403", response=body)
    assert (out.status, out.exit_code) == ("error", 0)
    assert "risk:write" in out.annotations[0]
    net = evaluate(http_status="000", response=None, message="curl: (7) refused")
    assert "Could not reach" in net.annotations[0]
    assert evaluate(http_status="500", response=None, fail_on_band="CRITICAL").exit_code == 1


def test_skipped_and_extract_failures() -> None:
    skipped = evaluate(stage="skipped", message="api-key is empty", response=None)
    assert (skipped.status, skipped.exit_code) == ("skipped", 0)
    failed = evaluate(stage="extract_failed", message="boom\nsecond line", response=None)
    assert (failed.status, failed.exit_code) == ("error", 0)
    assert "%0A" in failed.annotations[0]  # workflow-command escaping
    assert evaluate(stage="extract_failed", response=None, fail_on_band="HIGH").exit_code == 1


def test_untrusted_strings_are_sanitised() -> None:
    hostile = {
        **RESPONSE,
        "risk_band": "HIGH\n::error::injected",
        "details_url": "javascript:alert(1)",
        "top_increasing": [{"label": "<img src=x onerror=alert(1)>", "value": 1}],
    }
    out = evaluate(response=hostile)
    assert "\n" not in out.outputs["risk-band"]
    assert out.outputs["details-url"] == ""
    assert "<img" not in out.summary


def test_cli_writes_outputs_and_summary(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "stage").write_text("requested")
    (state / "http_status").write_text("201")
    (state / "response.json").write_text(json.dumps(RESPONSE))
    (state / "request.json").write_text(json.dumps(REQUEST))
    gh_out, summary = tmp_path / "out", tmp_path / "summary.md"
    code = rr.main(
        [
            "--state-dir", str(state), "--fail-on-band", "HIGH",
            "--github-output", str(gh_out), "--step-summary", str(summary),
        ]
    )  # fmt: skip
    assert code == 1  # HIGH >= HIGH
    assert "risk-score=78\n" in gh_out.read_text()
    assert "status=ok\n" in gh_out.read_text()
    assert rr.DISCLAIMER in summary.read_text()
