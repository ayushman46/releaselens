"""Conversion of the primary (5_3_2016) layout on its SYNTHETIC fixture."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from rl_pipeline.convert import MANIFEST_NAME, DataQualityError, DuckDBLimits, convert
from rl_pipeline.datasets import TT_5_3_2016
from rl_pipeline.hashing import md5_file
from rl_pipeline.schema import TT_5_3_2016_NAMES
from rl_pipeline.testing import build_rows_2016, write_csv_gz


def _all(result: dict[str, Any]) -> pl.DataFrame:
    return pl.concat(
        [pl.read_parquet(Path(result["local_dir"]) / f["path"]) for f in result["files"]]
    )


def _variant(tmp_path: Path, mutate: Any) -> tuple[Path, str]:
    rows = build_rows_2016()
    mutate(rows)
    path = write_csv_gz(tmp_path / "variant.csv.gz", rows, list(TT_5_3_2016_NAMES))
    return path, md5_file(path)


def test_layout_and_manifest_extras(converted_2016: dict[str, Any]) -> None:
    rows = build_rows_2016()
    assert converted_2016["dataset"] == TT_5_3_2016.key
    assert converted_2016["rows_total"] == len(rows) == 26
    assert [f["lang"] for f in converted_2016["files"]] == ["java", "ruby"]
    assert converted_2016["rows_per_lang"] == dict(Counter(r["gh_lang"] for r in rows))
    assert converted_2016["cast_failures_total"] == 0
    manifest = json.loads((Path(converted_2016["local_dir"]) / MANIFEST_NAME).read_text())
    assert manifest["dataset"] == "travistorrent"
    assert manifest["dataset_key"] == TT_5_3_2016.key
    assert manifest["converter"]["order_by"] == ["tr_build_id", "tr_job_id", "row"]
    assert manifest["column_patterns"] == {"tr_prev_build": r"[+-]?[0-9]+(\.0+)?"}
    assert "Europe/London" in manifest["timestamp_semantics"]
    key = manifest["key_profile"]
    assert key["unique_column"] == "row"
    assert key["duplicate_unique_keys"] == 0
    # two foreign job rows (one per java project) duplicate a tr_job_id: allowed, recorded
    assert key["duplicate_job_ids"] == 2
    assert manifest["source"] == {}  # convert() called without a source here


def test_types_placeholders_and_nulls(converted_2016: dict[str, Any]) -> None:
    df = _all(converted_2016)
    assert df.columns == list(TT_5_3_2016_NAMES)
    assert df.schema["row"] == pl.Int32
    assert df.schema["tr_prev_build"] == pl.Int64
    assert df.schema["tr_ci_latency"] == pl.Int64
    assert df.schema["tr_started_at"] == pl.Datetime("us")
    assert df.schema["tr_tests_ran"] == pl.Boolean
    raw = {r["row"]: r for r in build_rows_2016()}
    for rec in df.iter_rows(named=True):
        src = raw[str(rec["row"])]
        # "-1.0" and "-1" both stay -1 (no placeholder remapping in raw); "123.0" -> 123
        assert rec["tr_prev_build"] == int(float(src["tr_prev_build"]))
        assert rec["tr_ci_latency"] == int(src["tr_ci_latency"])
        assert rec["gh_is_pr"] is (src["gh_is_pr"] == "true")
        assert (rec["gh_pull_req_num"] is None) == (src["gh_pull_req_num"] == "NA")
        assert (rec["git_merged_with"] is None) == (src["git_merged_with"] == "")
        assert (rec["tr_purebuildduration"] is None) == (src["tr_purebuildduration"] == "")
    assert -1 in df["tr_prev_build"].to_list()
    assert -1 in df["tr_ci_latency"].to_list()
    texts = [t for t in df["tr_failed_tests"].to_list() if t]
    assert all(t.endswith('#test, with "quotes"') for t in texts)


def test_duplicates_are_kept_and_order_is_total(converted_2016: dict[str, Any]) -> None:
    for f in converted_2016["files"]:
        df = pl.read_parquet(Path(converted_2016["local_dir"]) / f["path"])
        keys = list(
            zip(
                df["tr_build_id"].to_list(),
                df["tr_job_id"].to_list(),
                df["row"].to_list(),
                strict=True,
            )
        )
        assert keys == sorted(keys)
    df = _all(converted_2016)
    assert df["row"].n_unique() == df.height
    assert df["tr_job_id"].n_unique() == df.height - 2  # raw is not deduplicated


def test_conversion_is_deterministic(
    tmp_path: Path, fixture_csv_2016: Path, fixture_version_2016: str, limits: DuckDBLimits
) -> None:
    a = convert(
        fixture_csv_2016, fixture_version_2016, tmp_path / "a", dataset=TT_5_3_2016, limits=limits
    )
    b = convert(
        fixture_csv_2016,
        fixture_version_2016,
        tmp_path / "b",
        dataset=TT_5_3_2016,
        limits=DuckDBLimits("512MB", 1),
    )
    assert [f["sha256"] for f in a["files"]] == [f["sha256"] for f in b["files"]]
    assert a["manifest_sha256"] == b["manifest_sha256"]
    again = convert(
        fixture_csv_2016, fixture_version_2016, tmp_path / "a", dataset=TT_5_3_2016, limits=limits
    )
    assert again["skipped"] is True


def test_duplicate_row_number_fails_the_gate(tmp_path: Path, limits: DuckDBLimits) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[1]["row"] = rows[0]["row"]

    path, version = _variant(tmp_path, mutate)
    with pytest.raises(DataQualityError, match="1 duplicate row, 0 null row"):
        convert(path, version, tmp_path / "out", dataset=TT_5_3_2016, limits=limits)


def test_prev_build_override_is_still_strict(tmp_path: Path, limits: DuckDBLimits) -> None:
    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["tr_prev_build"] = "12.5"  # not an exact integer: never rounded
        rows[1]["tr_prev_build"] = "1e3"
        rows[2]["tr_prev_build"] = "77.00"  # exact: accepted
        rows[3]["gh_team_size"] = "3.0"  # no override on other integer columns

    path, version = _variant(tmp_path, mutate)
    result = convert(
        path,
        version,
        tmp_path / "out",
        dataset=TT_5_3_2016,
        limits=limits,
        max_cast_failure_rate=0.5,
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    cols = manifest["cast_profile"]["columns"]
    assert cols["tr_prev_build"]["cast_failures"] == 2
    assert cols["gh_team_size"]["cast_failures"] == 1
    df = _all(result)
    assert 77 in df["tr_prev_build"].to_list()
    assert df.filter(pl.col("row") == int(build_rows_2016()[0]["row"]))[
        "tr_prev_build"
    ].to_list() == [None]


def test_shifted_row_is_counted_not_dropped(tmp_path: Path, limits: DuckDBLimits) -> None:
    """Like the 12 real shifted rows: unparsable values become NULL, the row stays."""

    def mutate(rows: list[dict[str, str]]) -> None:
        rows[0]["tr_tests_ran"] = "a260@a296"
        rows[0]["tr_num_jobs"] = "#[INFO]"

    path, version = _variant(tmp_path, mutate)
    result = convert(
        path,
        version,
        tmp_path / "out",
        dataset=TT_5_3_2016,
        limits=limits,
        max_cast_failure_rate=0.5,
    )
    assert result["rows_total"] == 26
    assert result["cast_failures_total"] == 2
