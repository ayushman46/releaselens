#!/usr/bin/env python3
"""Turn the ReleaseLens API answer into step outputs, a job summary and the fail-on-band gate.

Reads the files the earlier action steps left in ``--state-dir``:

* ``stage``: ``ready`` | ``skipped`` | ``extract_failed`` | ``requested``
* ``message``: why a stage stopped early (optional)
* ``request.json`` / ``diagnostics.json``: written by ``extract_change_facts.py``
* ``http_status`` / ``response.json``: written by ``scripts/request.sh``

Python 3.9+, standard library only. It never sees the API key.
Exit code 1 only when the gate trips, or when ``fail-on-band`` is set and no score could be
obtained (fail closed). Without a gate, API or extraction problems are annotations, not failures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BANDS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
GATES = ("none", "HIGH", "CRITICAL")
DISCLAIMER = (
    "This is a CI-failure risk estimate from historical patterns, "
    "not a production outage probability."
)
TITLE = "ReleaseLens"
MAX_SIGNALS = 5

FACT_LABELS = {
    "src_churn": "Source lines changed",
    "test_churn": "Test lines changed",
    "files_added": "Files added",
    "files_deleted": "Files deleted",
    "files_modified": "Files modified",
    "tests_added": "Test cases added",
    "tests_deleted": "Test cases deleted",
    "src_files": "Code files touched",
    "doc_files": "Documentation files touched",
    "other_files": "Other files touched",
    "sloc": "Source lines of code (repository)",
    "test_lines_per_kloc": "Test lines per 1,000 source lines",
    "test_cases_per_kloc": "Test cases per 1,000 source lines",
    "asserts_per_kloc": "Asserts per 1,000 source lines",
    "num_all_built_commits": "Commits in this build",
    "team_size": "Active developers (90 days)",
    "by_core_team_member": "Authored by active developers",
    "is_pr": "Pull request",
    "commits_on_files_touched": "Recent commits on touched files (90 days)",
    "repo_age_days": "Repository age (days)",
}


@dataclass
class Outcome:
    status: str  # ok | unavailable | error | skipped
    exit_code: int
    outputs: dict[str, str]
    annotations: list[str] = field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------- formatting


def _escape_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(kind: str, message: str) -> str:
    """A workflow command (``::notice::`` / ``::warning::`` / ``::error::``)."""
    return f"::{kind} title={TITLE}::{_escape_command(message)}"


def _md(value: Any, limit: int = 200) -> str:
    """Single-line, table-safe Markdown text from untrusted API strings."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return _md(value, 60)


def _single_line(value: Any) -> str:
    return re.sub(r"[\r\n]+", " ", "" if value is None else str(value)).strip()


def _safe_url(value: Any) -> str:
    url = _single_line(value)
    return url if re.match(r"\Ahttps?://[^\s()<>]+\Z", url) else ""


def _signals(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "none"
    parts = []
    for item in items[:MAX_SIGNALS]:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("feature") or "signal"
        value = item.get("value")
        shown = f"{_md(label, 80)} ({_fmt_value(value)})" if value is not None else _md(label, 80)
        parts.append(shown)
    return "; ".join(parts) if parts else "none"


def _facts_section(request: dict[str, Any] | None, diagnostics: dict[str, Any] | None) -> str:
    facts = (request or {}).get("change_facts")
    if not isinstance(facts, dict):
        return ""
    reasons = (diagnostics or {}).get("unavailable") or {}
    lines = [
        "<details><summary>Change facts sent (aggregate counts only)</summary>",
        "",
        "| Fact | Value |",
        "|---|---:|",
    ]
    for key, value in facts.items():
        shown = _fmt_value(value)
        if value is None and isinstance(reasons, dict) and key in reasons:
            shown = f"unknown ({_md(reasons[key], 80)})"
        lines.append(f"| {_md(FACT_LABELS.get(key, key), 80)} | {shown} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def _summary(
    headline: str,
    rows: Sequence[tuple[str, str]],
    request: dict[str, Any] | None,
    diagnostics: dict[str, Any] | None,
) -> str:
    out = [f"### {TITLE}: CI-failure risk", "", headline, ""]
    if rows:
        out += ["| | |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in rows]
        out.append("")
    out += [f"> {DISCLAIMER}", ""]
    facts = _facts_section(request, diagnostics)
    if facts:
        out.append(facts)
    return "\n".join(out)


# --------------------------------------------------------------------------- decision


def _gate_failure(fail_on: str, why: str) -> tuple[int, list[str]]:
    if fail_on == "none":
        return 0, []
    return 1, [annotation("error", f"fail-on-band is {fail_on} but {why}; failing closed.")]


def evaluate(
    *,
    stage: str,
    message: str,
    http_status: str,
    response: Any,
    request: dict[str, Any] | None,
    diagnostics: dict[str, Any] | None,
    fail_on_band: str,
) -> Outcome:
    gate = fail_on_band.strip().upper() if fail_on_band.strip().lower() != "none" else "none"
    empty = {"risk-score": "", "risk-band": "", "assessment-id": "", "details-url": ""}
    if gate not in GATES:
        msg = f"fail-on-band must be one of none, HIGH, CRITICAL (got {fail_on_band!r})"
        return Outcome("error", 1, {**empty, "status": "error"}, [annotation("error", msg)])

    if stage == "skipped":
        text = message or "no API key provided"
        code, notes = _gate_failure(gate, "no score was requested")
        summary = _summary(f"Skipped: {_md(text)}.", (), request, diagnostics)
        return Outcome(
            "skipped",
            code,
            {**empty, "status": "skipped"},
            [annotation("notice", text), *notes],
            summary,
        )

    if stage != "requested":
        text = message or "change facts could not be extracted"
        code, notes = _gate_failure(gate, "no score is available")
        summary = _summary(f"No score: {_md(text)}.", (), request, diagnostics)
        return Outcome(
            "error",
            code,
            {**empty, "status": "error"},
            [annotation("warning", text), *notes],
            summary,
        )

    status = http_status.strip() or "000"
    body = response if isinstance(response, dict) else {}
    if status.startswith("2") and isinstance(body.get("risk_score"), (int, float)):
        return _scored(body, gate, request, diagnostics)

    if status == "503":
        text = (
            "The ReleaseLens API has no model loaded yet (HTTP 503), so there is no risk score "
            "for this change."
        )
        if gate == "none":
            text += " The build is not affected."
        code, notes = _gate_failure(gate, "no model is loaded")
        summary = _summary(_md(text), (), request, diagnostics)
        return Outcome(
            "unavailable",
            code,
            {**empty, "status": "unavailable"},
            [annotation("notice", text), *notes],
            summary,
        )

    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    detail = ""
    if err:
        detail = f": {_single_line(err.get('code'))}: {_single_line(err.get('message'))}"
    elif message:
        detail = f": {_single_line(message)}"
    if status == "000":
        text = f"Could not reach the ReleaseLens API{detail}"
    elif status in ("401", "403"):
        text = (
            f"The ReleaseLens API rejected the key (HTTP {status}{detail}); "
            "it needs scope risk:write"
        )
    else:
        text = f"The ReleaseLens API returned HTTP {status}{detail}"
    code, notes = _gate_failure(gate, "no score is available")
    summary = _summary(f"No score: {_md(text)}.", (), request, diagnostics)
    return Outcome(
        "error", code, {**empty, "status": "error"}, [annotation("warning", text), *notes], summary
    )


def _scored(
    body: dict[str, Any],
    gate: str,
    request: dict[str, Any] | None,
    diagnostics: dict[str, Any] | None,
) -> Outcome:
    score = round(float(body["risk_score"]))
    band = _single_line(body.get("risk_band")).upper()
    details_url = _safe_url(body.get("details_url"))
    outputs = {
        "risk-score": str(score),
        "risk-band": band,
        "assessment-id": _single_line(body.get("assessment_id")),
        "details-url": details_url,
        "status": "ok",
    }
    rows: list[tuple[str, str]] = [
        ("Risk score", f"**{score}** / 100 (percentile among validation builds)"),
        ("Risk band", f"**{_md(band, 20)}**" if band else "unknown"),
        ("Top signals raising risk", _signals(body.get("top_increasing"))),
        ("Top signals lowering risk", _signals(body.get("top_reducing"))),
    ]
    prob = body.get("probability")
    if isinstance(prob, (int, float)) and not isinstance(prob, bool):
        rows.append(("Calibrated CI-failure probability", f"{float(prob) * 100:.0f}%"))
    history = body.get("history")
    if isinstance(history, dict) and history.get("cold_start"):
        used = history.get("finished_builds_used")
        rows.append(("Build history", f"cold start ({_fmt_value(used)} finished builds used)"))
    if body.get("model_version"):
        rows.append(("Model", _md(body["model_version"], 60)))
    if details_url:
        rows.append(("Details", f"[Open in ReleaseLens]({details_url})"))

    notes = [annotation("notice", f"CI-failure risk {score}/100 ({band or 'unknown band'})")]
    code = 0
    if gate != "none":
        if band not in BANDS:
            code, more = _gate_failure(gate, f"the API returned an unknown band {band!r}")
            notes += more
        elif BANDS.index(band) >= BANDS.index(gate):
            code = 1
            notes.append(
                annotation("error", f"Risk band {band} is at or above fail-on-band {gate}.")
            )
    summary = _summary(
        f"Risk **{score}/100**, band **{_md(band, 20)}**.", rows, request, diagnostics
    )
    return Outcome("ok", code, outputs, notes, summary)


# --------------------------------------------------------------------------- CLI


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_json(path: Path) -> Any:
    text = _read_text(path)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def write_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={_single_line(value)}\n")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="render_result.py", description=__doc__.splitlines()[0])
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--fail-on-band", default="none")
    p.add_argument("--github-output", type=Path, default=None, help="default: $GITHUB_OUTPUT")
    p.add_argument("--step-summary", type=Path, default=None, help="default: $GITHUB_STEP_SUMMARY")
    args = p.parse_args(argv)

    state: Path = args.state_dir
    request = _read_json(state / "request.json")
    diagnostics = _read_json(state / "diagnostics.json")
    outcome = evaluate(
        stage=_read_text(state / "stage"),
        message=_read_text(state / "message"),
        http_status=_read_text(state / "http_status"),
        response=_read_json(state / "response.json"),
        request=request if isinstance(request, dict) else None,
        diagnostics=diagnostics if isinstance(diagnostics, dict) else None,
        fail_on_band=args.fail_on_band,
    )
    for line in outcome.annotations:
        sys.stdout.write(line + "\n")
    out_path = args.github_output or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )
    if out_path is not None:
        write_outputs(out_path, outcome.outputs)
    summary_path = args.step_summary or (
        Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None
    )
    if summary_path is not None and outcome.summary:
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(outcome.summary + "\n")
    elif outcome.summary:
        sys.stdout.write(outcome.summary + "\n")
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
