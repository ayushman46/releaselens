from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from rl_pipeline.convert import (
    MANIFEST_NAME,
    DataQualityError,
    DuckDBLimits,
    InsufficientDiskError,
    connect,
    convert,
    temp_budget,
    typed_expr,
)
from rl_pipeline.datasets import TT_FINAL_2017 as V17
from rl_pipeline.hashing import md5_file
from rl_pipeline.schema import FINAL_2017_NAMES as EXPECTED_NAMES
from rl_pipeline.schema import SchemaMismatchError
from rl_pipeline.testing import build_rows, write_csv_gz


def _shas(result: dict[str, Any]) -> dict[str, str]:
    return {f["path"]: f["sha256"] for f in result["files"]}


def _read(result: dict[str, Any], lang: str) -> pl.DataFrame:
    return pl.read_parquet(Path(result["local_dir"]) / f"lang={lang}" / "part-00000.parquet")


def test_layout_rows_and_manifest(converted: dict[str, Any]) -> None:
    rows = build_rows()
    local = Path(converted["local_dir"])
    assert local.name == f"version={converted['version']}"
    assert converted["skipped"] is False
    assert converted["rows_total"] == len(rows) == 30
    expected_langs = sorted({r["gh_lang"] for r in rows})
    assert [f["lang"] for f in converted["files"]] == expected_langs
    for f in converted["files"]:
        assert f["path"] == f"lang={f['lang']}/part-00000.parquet"
        assert f["rows"] == sum(r["gh_lang"] == f["lang"] for r in rows)
        assert (local / f["path"]).stat().st_size == f["size"]
    manifest = json.loads((local / MANIFEST_NAME).read_text())
    assert manifest["version"] == converted["version"]
    assert manifest["files"] == converted["files"]
    assert manifest["cast_profile"]["columns"]["gh_team_size"]["cast_failures"] == 0
    assert converted["cast_failures_total"] == 0
    assert "conversion_seconds" not in manifest  # manifest must stay deterministic


def test_types_nulls_and_numeric_sort_order(converted: dict[str, Any]) -> None:
    df = _read(converted, "java")
    assert df.columns == list(EXPECTED_NAMES)
    assert df.schema["tr_build_id"] == pl.Int64
    assert df.schema["gh_team_size"] == pl.Int32
    assert df.schema["gh_is_pr"] == pl.Boolean
    assert df.schema["gh_build_started_at"] == pl.Datetime("us")
    assert df.schema["gh_repo_age"] == pl.Float64
    # numeric (not lexicographic) order by (tr_build_id, tr_job_id)
    keys = list(zip(df["tr_build_id"].to_list(), df["tr_job_id"].to_list(), strict=True))
    assert keys == sorted(keys)
    assert df["tr_build_id"].to_list()[0] == 5
    assert 99 in df["tr_build_id"].to_list()
    assert 20000 in df["tr_build_id"].to_list()
    # NA tokens and empty fields become NULL
    assert df["gh_num_commits_in_push"].null_count() == df.height
    assert df["gh_commits_in_push"].null_count() == df.height
    non_pr = df.filter(~pl.col("gh_is_pr"))
    assert non_pr["gh_pull_req_num"].null_count() == non_pr.height
    assert non_pr["git_merged_with"].null_count() == non_pr.height
    assert isinstance(df["gh_build_started_at"][0], datetime)
    # free text with a comma survives quoting intact
    texts = [t for t in df["tr_log_tests_failed"].to_list() if t]
    assert all(t.endswith("#test, with comma") for t in texts)


def test_conversion_is_deterministic(
    tmp_path: Path, fixture_csv: Path, fixture_version: str, limits: DuckDBLimits
) -> None:
    a = convert(fixture_csv, fixture_version, tmp_path / "a", dataset=V17, limits=limits)
    b = convert(
        fixture_csv, fixture_version, tmp_path / "b", dataset=V17, limits=DuckDBLimits("512MB", 1)
    )
    assert _shas(a) == _shas(b)
    assert a["manifest_sha256"] == b["manifest_sha256"]


def test_skip_path_and_regeneration_after_manifest_delete(
    tmp_path: Path, fixture_csv: Path, fixture_version: str, limits: DuckDBLimits
) -> None:
    first = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    local = Path(first["local_dir"])
    mtimes = {p: p.stat().st_mtime_ns for p in local.rglob("*") if p.is_file()}

    second = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    assert second["skipped"] is True
    assert _shas(second) == _shas(first)
    assert {p: p.stat().st_mtime_ns for p in local.rglob("*") if p.is_file()} == mtimes

    (local / MANIFEST_NAME).unlink()
    third = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    assert third["skipped"] is False
    assert _shas(third) == _shas(first)
    assert third["manifest_sha256"] == first["manifest_sha256"]
    assert not list(tmp_path.glob(".staging-*"))
    assert not list(tmp_path.glob(".old-*"))
    assert not list(tmp_path.glob(".duckdb_tmp-*"))


def test_tampered_file_triggers_regeneration(
    tmp_path: Path, fixture_csv: Path, fixture_version: str, limits: DuckDBLimits
) -> None:
    first = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    target = Path(first["local_dir"]) / first["files"][0]["path"]
    target.write_bytes(b"not parquet")
    again = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    assert again["skipped"] is False
    assert _shas(again) == _shas(first)


def _variant(tmp_path: Path, mutate: Any) -> tuple[Path, str]:
    rows = build_rows()
    mutate(rows)
    path = write_csv_gz(tmp_path / "variant.csv.gz", rows)
    return path, md5_file(path)


def test_cast_failure_gate_trips(tmp_path: Path, limits: DuckDBLimits) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[3]["gh_team_size"] = "twelve"
        rows[4]["gh_build_started_at"] = "2014-02-30T25:61:00Z"

    path, version = _variant(tmp_path, mutate)
    with pytest.raises(DataQualityError) as err:
        convert(path, version, tmp_path / "out", dataset=V17, limits=limits)
    msg = str(err.value)
    assert "gh_team_size (INTEGER): 1 of 30" in msg
    assert "gh_build_started_at (TIMESTAMP): 1 of 30" in msg
    assert not (tmp_path / "out" / f"version={version}").exists()


def test_cast_failures_below_threshold_are_recorded(tmp_path: Path, limits: DuckDBLimits) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["gh_sloc"] = "lots"

    path, version = _variant(tmp_path, mutate)
    result = convert(
        path, version, tmp_path / "out", dataset=V17, limits=limits, max_cast_failure_rate=0.5
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    col = manifest["cast_profile"]["columns"]["gh_sloc"]
    assert col["cast_failures"] == 1
    assert col["non_missing"] == 30
    assert result["cast_failures_total"] == 1


@pytest.mark.parametrize("bad_lang", ["NA", "../escape", "Java Script"])
def test_unsafe_or_missing_lang_is_rejected(
    tmp_path: Path, limits: DuckDBLimits, bad_lang: str
) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["gh_lang"] = bad_lang

    path, version = _variant(tmp_path, mutate)
    with pytest.raises(DataQualityError, match="gh_lang"):
        convert(path, version, tmp_path / "out", dataset=V17, limits=limits)


def test_duplicate_job_id_is_rejected(tmp_path: Path, limits: DuckDBLimits) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[1]["tr_job_id"] = rows[0]["tr_job_id"]

    path, version = _variant(tmp_path, mutate)
    with pytest.raises(DataQualityError, match="1 duplicate tr_job_id"):
        convert(path, version, tmp_path / "out", dataset=V17, limits=limits)


def test_header_mismatch_stops_conversion(tmp_path: Path, limits: DuckDBLimits) -> None:
    header = list(reversed(EXPECTED_NAMES))
    path = write_csv_gz(tmp_path / "rev.csv.gz", build_rows(), header)
    with pytest.raises(SchemaMismatchError):
        convert(path, md5_file(path), tmp_path / "out", dataset=V17, limits=limits)


def test_version_must_be_md5(tmp_path: Path, fixture_csv: Path) -> None:
    with pytest.raises(ValueError, match="md5"):
        convert(fixture_csv, "../../etc", tmp_path, dataset=V17)


def test_typed_expr_shapes() -> None:
    assert typed_expr("x", "VARCHAR").startswith("CASE WHEN")
    assert "TRY_STRPTIME" in typed_expr("x", "TIMESTAMP")
    assert "TRY_CAST" in typed_expr("x", "BIGINT")
    assert "regexp_full_match" in typed_expr("x", "BIGINT")
    assert "regexp_full_match" in typed_expr("x", "TIMESTAMP")
    assert "'TRUE' THEN TRUE" in typed_expr("x", "BOOLEAN")


# ---------------------------------------------------------------- review hardening
def test_lenient_duckdb_casts_are_rejected(tmp_path: Path, limits: DuckDBLimits) -> None:
    """TRY_CAST alone rounds '1.5' -> 2 and accepts hex/underscores/nan; strict casts must not."""

    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["gh_team_size"] = "1.5"
        rows[1]["gh_sloc"] = "0x10"
        rows[2]["gh_num_commits_on_files_touched"] = "1_000"
        rows[3]["gh_repo_age"] = "nan"
        rows[4]["gh_build_started_at"] = "2014-1-1 1:2:3"
        rows[5]["git_diff_src_churn"] = "1e3"

    path, version = _variant(tmp_path, mutate)
    result = convert(
        path, version, tmp_path / "out", dataset=V17, limits=limits, max_cast_failure_rate=0.5
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    cols = manifest["cast_profile"]["columns"]
    for name in (
        "gh_team_size",
        "gh_sloc",
        "gh_num_commits_on_files_touched",
        "gh_repo_age",
        "gh_build_started_at",
        "git_diff_src_churn",
    ):
        assert cols[name]["cast_failures"] == 1, name
    assert result["cast_failures_total"] == 6
    # ... and the malformed values are NULL in the output, not rounded/reinterpreted
    df = pl.concat(
        [pl.read_parquet(Path(result["local_dir"]) / f["path"]) for f in result["files"]]
    )
    assert (
        2
        not in df.filter(pl.col("tr_job_id") == int(build_rows()[0]["tr_job_id"]))[
            "gh_team_size"
        ].to_list()
    )
    assert df["gh_team_size"].null_count() == 1
    assert df["gh_repo_age"].null_count() == 1
    assert df["gh_build_started_at"].null_count() == 1


def test_strict_casts_accept_well_formed_values(tmp_path: Path, limits: DuckDBLimits) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["gh_team_size"] = "-3"
        rows[1]["gh_repo_age"] = "12"
        rows[2]["gh_repo_age"] = "1.5e2"
        rows[3]["gh_is_pr"] = "true"

    path, version = _variant(tmp_path, mutate)
    result = convert(path, version, tmp_path / "out", dataset=V17, limits=limits)
    assert result["cast_failures_total"] == 0


def test_failed_conversion_leaves_no_work_dirs(
    tmp_path: Path,
    fixture_csv: Path,
    fixture_version: str,
    limits: DuckDBLimits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rl_pipeline import convert as convert_mod

    def boom(path: Path, spec: Any) -> None:
        raise DataQualityError(f"simulated failure after writing {path.name}")

    monkeypatch.setattr(convert_mod, "_verify_parquet_schema", boom)
    with pytest.raises(DataQualityError, match="simulated"):
        convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_stale_work_dirs_of_dead_runs_are_swept(
    tmp_path: Path, fixture_csv: Path, fixture_version: str, limits: DuckDBLimits
) -> None:
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    gone, alive = proc.pid, os.getppid()
    stale = [
        tmp_path / f".staging-version={fixture_version}-{gone}",
        tmp_path / f".duckdb_tmp-{gone}",
        tmp_path / f".old-version={fixture_version}-{gone}",
    ]
    live = tmp_path / f".staging-version={fixture_version}-{alive}"
    unrelated = tmp_path / "keep-me"
    for d in [*stale, live, unrelated]:
        d.mkdir()
        (d / "x").write_bytes(b"spill")
    convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    assert not any(d.exists() for d in stale)
    assert live.exists()
    assert unrelated.exists()


def test_insufficient_disk_fails_before_writing(
    tmp_path: Path,
    fixture_csv: Path,
    fixture_version: str,
    limits: DuckDBLimits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    # convert.py calls shutil.disk_usage via the shared module object
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: usage(10**9, 10**9, 10**6))
    with pytest.raises(InsufficientDiskError, match="free"):
        convert(fixture_csv, fixture_version, tmp_path / "out", dataset=V17, limits=limits)
    assert not (tmp_path / "out" / f"version={fixture_version}").exists()


def test_spill_cap_is_applied(tmp_path: Path) -> None:
    lim = DuckDBLimits("256MB", 1, max_temp_mib=64)
    cap = temp_budget(tmp_path, 0, lim)
    assert cap == 64 * 1024 * 1024  # the configured cap wins when disk is plentiful
    con = connect(lim, tmp_path / "spill", max_temp_bytes=cap)
    try:
        row = con.execute("SELECT current_setting('max_temp_directory_size')").fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == "64.0 MiB"


def test_null_job_id_is_not_double_counted_as_duplicate(
    tmp_path: Path, limits: DuckDBLimits
) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["tr_job_id"] = "NA"

    path, version = _variant(tmp_path, mutate)
    with pytest.raises(DataQualityError, match="0 duplicate tr_job_id, 1 null tr_job_id"):
        convert(path, version, tmp_path / "out", dataset=V17, limits=limits)


def test_unreadable_manifest_triggers_regeneration(
    tmp_path: Path, fixture_csv: Path, fixture_version: str, limits: DuckDBLimits
) -> None:
    first = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    Path(first["manifest_path"]).write_text("{ truncated")
    again = convert(fixture_csv, fixture_version, tmp_path, dataset=V17, limits=limits)
    assert again["skipped"] is False
    assert again["manifest_sha256"] == first["manifest_sha256"]
