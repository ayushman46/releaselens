"""``python -m rl_analysis report --run-id <dbt run id> [options]``

Reads the run's marts and feature mart (default ``s3://<RL_S3_BUCKET_DATA>``, or a local output
root with ``--input-root``), writes ``findings.json`` and ``FINDINGS.md`` (default
``docs/analytics``) and uploads ``findings.json`` to
``s3://<RL_S3_BUCKET_DATA>/reports/analytics/run_id=<run id>/findings.json`` (``--no-upload`` to
skip). ``--duckdb-path`` additionally checks the naive-link mirror against dbt's
``int_build_history`` in that DuckDB file. A JSON summary is printed on the last stdout line;
exit code 1 on failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from rl_analysis.findings import AnalysisConfig
from rl_analysis.settings import AnalysisSettings, repo_root

log = logging.getLogger("rl_analysis")
_DEFAULTS = AnalysisConfig()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rl_analysis", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="compute the findings for one dbt run")
    rep.add_argument("--run-id", required=True, help="dbt run id (path segment run_id=<id>)")
    rep.add_argument("--input-root", help="s3://<bucket> or a local dbt output root")
    rep.add_argument("--out-dir", type=Path, help="default: <repo>/docs/analytics")
    rep.add_argument("--duckdb-path", type=Path, help="dbt DuckDB file to check int_build_history")
    rep.add_argument("--no-upload", action="store_true", help="do not upload findings.json")
    rep.add_argument("--generated-on", help="date printed in FINDINGS.md (default: today, UTC)")
    rep.add_argument("--min-project-builds", type=int, default=_DEFAULTS.min_project_builds)
    rep.add_argument("--min-month-builds", type=int, default=_DEFAULTS.min_month_builds)
    rep.add_argument("--top-n", type=int, default=_DEFAULTS.top_n)
    return parser


def canonical_command(args: argparse.Namespace, default_root: str) -> str:
    """The generation command as recorded in the report (options that change the content)."""
    parts = ["python -m rl_analysis report", f"--run-id {args.run_id}"]
    if args.input_root and args.input_root.rstrip("/") != default_root:
        parts.append(f"--input-root {args.input_root}")
    if args.duckdb_path is not None:
        parts.append(f"--duckdb-path {args.duckdb_path.as_posix()}")
    for flag, value, default in (
        ("--min-project-builds", args.min_project_builds, _DEFAULTS.min_project_builds),
        ("--min-month-builds", args.min_month_builds, _DEFAULTS.min_month_builds),
        ("--top-n", args.top_n, _DEFAULTS.top_n),
    ):
        if value != default:
            parts.append(f"{flag} {value}")
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    from rl_analysis.report import ReportRequest, build_report, dumps, write_outputs
    from rl_analysis.s3 import make_client, upload_report

    args = _parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = AnalysisSettings()
    root = args.input_root or settings.default_input_root
    request = ReportRequest(
        run_id=args.run_id,
        input_root=root,
        duckdb_path=args.duckdb_path,
        config=AnalysisConfig(
            min_project_builds=args.min_project_builds,
            min_month_builds=args.min_month_builds,
            top_n=args.top_n,
        ),
        command=canonical_command(args, settings.default_input_root),
    )
    generated_on = args.generated_on or dt.datetime.now(dt.UTC).date().isoformat()
    try:
        report = build_report(request, settings)
        written = write_outputs(
            report, args.out_dir or repo_root() / "docs" / "analytics", generated_on=generated_on
        )
        upload = None
        if not args.no_upload:
            upload = upload_report(
                make_client(settings), settings.s3_bucket_data, args.run_id, dumps(report)
            )
    except Exception:
        log.exception("report failed")
        return 1
    summary = {"run_id": args.run_id, **written, "upload": upload}
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
