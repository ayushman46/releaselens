"""Report assembly on the SYNTHETIC fixture: determinism, CLI, checks, export, upload."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from rl_analysis.__main__ import main
from rl_analysis.export import NAMESPACE, rows_for_postgres
from rl_analysis.findings import DBT_CHURN_LABELS, CheckError
from rl_analysis.inputs import InputError
from rl_analysis.markdown import render_markdown
from rl_analysis.report import FINDING_ORDER, dumps
from rl_analysis.s3 import report_key, upload_report
from rl_analysis.testing import SYNTHETIC_NAIVE, write_synthetic_dbt_duckdb, write_synthetic_run

Report = dict[str, Any]
CLI_SMALL = ["--min-project-builds", "3", "--min-month-builds", "7", "--top-n", "1"]


def test_report_is_deterministic(make_report: Callable[..., Report]) -> None:
    first, second = make_report(), make_report()
    assert dumps(first) == dumps(second)
    assert render_markdown(first, generated_on="2026-10-13") == render_markdown(
        second, generated_on="2026-10-13"
    )


def test_cli_twice_writes_identical_files(
    synthetic_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs = []
    for name in ("a", "b"):
        out = tmp_path / name
        argv = [
            "report",
            "--run-id",
            "synthetic",
            "--input-root",
            str(synthetic_root),
            "--out-dir",
            str(out),
            "--no-upload",
            "--generated-on",
            "2026-10-13",
            *CLI_SMALL,
        ]
        assert main(argv) == 0
        summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert summary["upload"] is None
        assert summary["sha256"] == hashlib.sha256((out / "findings.json").read_bytes()).hexdigest()
        outputs.append(((out / "findings.json").read_bytes(), (out / "FINDINGS.md").read_text()))
    assert outputs[0] == outputs[1]
    report = json.loads(outputs[0][0])
    assert report["generated_by"] == (
        f"python -m rl_analysis report --run-id synthetic --input-root {synthetic_root} "
        "--min-project-builds 3 --min-month-builds 7 --top-n 1"
    )


def test_cli_fails_cleanly_on_a_missing_run(synthetic_root: Path, tmp_path: Path) -> None:
    argv = [
        "report",
        "--run-id",
        "nope",
        "--input-root",
        str(synthetic_root),
        "--out-dir",
        str(tmp_path / "x"),
        "--no-upload",
    ]
    assert main(argv) == 1
    assert not (tmp_path / "x" / "findings.json").exists()


def test_stable_top_level_schema(report: Report) -> None:
    assert list(report) == [
        "report",
        "schema_version",
        "run_id",
        "generated_by",
        "dataset",
        "inputs",
        "environment",
        "config",
        "checks",
        "population",
        "findings",
        "chi_square_tests",
        "stratified_tests",
        "caveats",
    ]
    assert [f["key"] for f in report["findings"]] == list(FINDING_ORDER)
    required = {
        "key",
        "spec_ref",
        "title",
        "question",
        "population",
        "series",
        "tests",
        "headline",
        "summary",
        "caveats",
    }
    for f in report["findings"]:
        assert required <= set(f), f["key"]
    assert report["inputs"]["files"]["mart_build_features"]["rows"] == 16
    json.loads(dumps(report))  # strict JSON: no NaN/inf


def test_tampered_mart_fails_the_cross_check(tmp_path: Path, make_report: Any) -> None:
    from rl_analysis.report import ReportRequest, build_report
    from rl_analysis.settings import AnalysisSettings
    from rl_analysis.testing import _marts_from_builds, builds_frame

    change, _ = _marts_from_builds(builds_frame())
    change = change.with_columns(
        pl.when(pl.col("bucket") == DBT_CHURN_LABELS[0])
        .then(pl.col("n_failures") + 1)
        .otherwise(pl.col("n_failures"))
        .alias("n_failures")
    )
    root = write_synthetic_run(tmp_path / "bad", change_risk=change)
    with pytest.raises(CheckError, match="change_size_buckets_disagreeing"):
        build_report(ReportRequest(run_id="synthetic", input_root=str(root)), AnalysisSettings())


def test_month_with_only_unlabelled_builds_passes_the_checks(tmp_path: Path) -> None:
    """dbt's reliability mart lists such a month with 0 labelled builds (found on the dbt
    fixture); it must not count as a disagreement."""
    from rl_analysis.report import ReportRequest, build_report
    from rl_analysis.settings import AnalysisSettings
    from rl_analysis.testing import builds_frame

    builds = builds_frame()
    extra = builds.filter(pl.col("build_id") == 203).with_columns(
        pl.lit(209, dtype=pl.Int64).alias("build_id"),
        (pl.col("started_at") + pl.duration(days=60)).alias("started_at"),
        (pl.col("finished_at_upper") + pl.duration(days=60)).alias("finished_at_upper"),
        pl.lit(dt.date(2015, 3, 1)).alias("started_month"),
    )
    root = write_synthetic_run(tmp_path / "march", builds=pl.concat([builds, extra]))
    report = build_report(
        ReportRequest(run_id="synthetic", input_root=str(root)), AnalysisSettings()
    )
    assert set(report["checks"]["cross_checks"].values()) == {0}
    assert report["population"]["builds"] == 18
    assert report["population"]["labelled_builds"] == 16


def test_manifest_mismatch_is_rejected(synthetic_root: Path, make_report: Any) -> None:
    manifest = next(synthetic_root.glob("features/**/_manifest.json"))
    data = json.loads(manifest.read_text())
    data["file"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(InputError, match="sha256"):
        make_report()


def test_naive_link_check(
    make_report: Callable[..., Report], synthetic_duckdb: Path, tmp_path: Path
) -> None:
    report = make_report(duckdb_path=synthetic_duckdb)
    check = report["checks"]["naive_link_vs_int_build_history"]
    assert check == {
        "status": "ok",
        "compared_with": "int_build_history",
        "duckdb_file": "releaselens.duckdb",
        "rows": 17,
        "missing_rows": 0,
        "mismatched_rows": 0,
    }
    wrong = dict(SYNTHETIC_NAIVE)
    wrong[107] = (105, 1, True)  # the leakage-safe predecessor, not the naive one
    bad = write_synthetic_dbt_duckdb(tmp_path / "bad.duckdb", wrong)
    with pytest.raises(RuntimeError, match="mismatched_rows': 1"):
        make_report(duckdb_path=bad)


def test_markdown_mentions_every_headline_number(report: Report) -> None:
    md = render_markdown(report, generated_on="2026-10-13")
    assert "2026-10-13" in md
    assert report["dataset"]["version"] in md
    assert report["generated_by"] in md
    for f in report["findings"]:
        assert f["summary"] in md
        assert f["title"] in md
    for t in report["chi_square_tests"]["tests"]:
        assert f"`{t['key']}`" in md


def test_rows_for_postgres(report: Report) -> None:
    rows = rows_for_postgres(report)
    assert set(rows) == {"analytics_runs", "analytics_findings"}
    (run,) = rows["analytics_runs"]
    assert run["run_id"] == "synthetic"
    assert run["id"] == str(uuid.uuid5(NAMESPACE, "synthetic"))
    assert run["n_labelled_builds"] == 16
    assert run["n_failures"] == 7
    assert run["findings_sha256"] == hashlib.sha256(dumps(report)).hexdigest()
    findings = rows["analytics_findings"]
    assert [r["finding_key"] for r in findings] == list(FINDING_ORDER)
    assert [r["ordinal"] for r in findings] == list(range(1, len(FINDING_ORDER) + 1))
    assert len({r["id"] for r in findings}) == len(findings)
    change = findings[0]
    assert change["test_key"] == "change_size"
    assert change["effect_name"] == "cramers_v"
    assert change["n"] == 16
    duration = next(r for r in findings if r["finding_key"] == "duration")
    assert duration["test_name"] == "mann_whitney_u"
    assert duration["p_value_holm"] is None
    assert rows_for_postgres(report) == rows  # same ids on re-export


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.puts = 0

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 API
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": self.objects[Key]["Metadata"]}

    def put_object(self, **kwargs: Any) -> None:
        self.puts += 1
        self.objects[kwargs["Key"]] = kwargs


def test_upload_is_idempotent(report: Report) -> None:
    client, body = _FakeS3(), dumps(report)
    first = upload_report(client, "bucket", "synthetic", body)
    second = upload_report(client, "bucket", "synthetic", body)
    assert first["uploaded"] is True
    assert second["uploaded"] is False
    assert client.puts == 1
    key = report_key("synthetic")
    assert key == "reports/analytics/run_id=synthetic/findings.json"
    assert first["location"] == f"s3://bucket/{key}"
    assert client.objects[key]["Metadata"] == {"sha256": hashlib.sha256(body).hexdigest()}
    assert client.objects[key]["ContentType"] == "application/json"
