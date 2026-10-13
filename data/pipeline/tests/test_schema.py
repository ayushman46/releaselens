from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import duckdb
import pytest

from rl_pipeline.datasets import TT_5_3_2016, TT_FINAL_2017
from rl_pipeline.schema import FINAL_2017_COLUMNS as EXPECTED_COLUMNS
from rl_pipeline.schema import FINAL_2017_NAMES as EXPECTED_NAMES
from rl_pipeline.schema import (
    TT_5_3_2016_COLUMNS,
    TT_5_3_2016_NAMES,
    SchemaMismatchError,
    diff_header,
    read_header,
    schema_fingerprint,
    validate_header,
)
from rl_pipeline.tasks import validate_raw_schema as _validate_raw_schema
from rl_pipeline.testing import build_rows, build_rows_2016, render_csv, write_csv_gz

COLUMN_TYPES = dict(EXPECTED_COLUMNS)
V17 = TT_FINAL_2017.key


def validate_raw_schema(dataset: dict[str, Any]) -> dict[str, Any]:
    """The 2017-layout fixture needs the secondary dataset key (the default is the primary)."""
    return _validate_raw_schema({"dataset": V17, **dataset})


def test_expected_schema_shape() -> None:
    assert len(EXPECTED_COLUMNS) == 66
    assert len(set(EXPECTED_NAMES)) == 66
    assert EXPECTED_NAMES[0] == "tr_build_id"
    assert EXPECTED_NAMES[-1] == "tr_jobs"
    assert set(COLUMN_TYPES.values()) <= {
        "BIGINT",
        "INTEGER",
        "DOUBLE",
        "BOOLEAN",
        "VARCHAR",
        "TIMESTAMP",
    }
    assert len(schema_fingerprint(EXPECTED_COLUMNS)) == 64


def test_fixture_matches_generator(fixture_csv: Path) -> None:
    """The committed synthetic fixture must be exactly what the generator produces."""
    assert gzip.decompress(fixture_csv.read_bytes()) == render_csv(build_rows())


def test_fixture_is_30_synthetic_rows() -> None:
    rows = build_rows()
    assert len(rows) == 30
    assert all(r["gh_project_name"].startswith("synthetic-org/") for r in rows)


def test_read_header_returns_raw_names(fixture_csv: Path) -> None:
    con = duckdb.connect()
    try:
        assert read_header(con, fixture_csv) == list(EXPECTED_NAMES)
    finally:
        con.close()


def test_validate_task_passes_on_fixture(fixture_csv: Path, fixture_version: str) -> None:
    out = validate_raw_schema(
        {"path": str(fixture_csv), "version": fixture_version, "size": fixture_csv.stat().st_size}
    )
    assert out["ok"] is True
    assert out["columns"] == 66
    assert out["version"] == fixture_version


def test_missing_column_fails_loudly(tmp_path: Path) -> None:
    header = [n for n in EXPECTED_NAMES if n != "gh_team_size"]
    path = write_csv_gz(tmp_path / "missing.csv.gz", build_rows(), header)
    with pytest.raises(SchemaMismatchError) as err:
        validate_raw_schema({"path": str(path), "version": "x" * 32})
    msg = str(err.value)
    assert "missing columns: ['gh_team_size']" in msg
    assert "expected 66 columns, found 65" in msg


def test_reordered_columns_fail_loudly(tmp_path: Path) -> None:
    header = list(EXPECTED_NAMES)
    header[1], header[2] = header[2], header[1]
    path = write_csv_gz(tmp_path / "reordered.csv.gz", build_rows(), header)
    with pytest.raises(SchemaMismatchError) as err:
        validate_raw_schema({"path": str(path), "version": "x" * 32})
    assert "first order mismatch at position 1" in str(err.value)
    assert "missing columns" not in str(err.value)


def test_extra_and_duplicate_columns_are_reported() -> None:
    problems = diff_header([*EXPECTED_NAMES, "surprise", "tr_jobs"], EXPECTED_NAMES)
    joined = "; ".join(problems)
    assert "unexpected columns: ['surprise']" in joined
    assert "duplicate columns: ['tr_jobs']" in joined


def test_exact_header_has_no_diff() -> None:
    assert diff_header(list(EXPECTED_NAMES), EXPECTED_NAMES) == []
    validate_header(list(EXPECTED_NAMES), EXPECTED_NAMES)


def test_size_mismatch_is_rejected(fixture_csv: Path) -> None:
    with pytest.raises(Exception, match="bytes, expected"):
        validate_raw_schema({"path": str(fixture_csv), "version": "x" * 32, "size": 1})


# ---------------------------------------------------------------- 5_3_2016 (primary) layout
def test_2016_schema_shape() -> None:
    assert len(TT_5_3_2016_COLUMNS) == 56
    assert TT_5_3_2016_NAMES[0] == "row"
    assert TT_5_3_2016_NAMES[-1] == "tr_ci_latency"
    assert TT_5_3_2016.names == TT_5_3_2016_NAMES


def test_fixture_2016_matches_generator(fixture_csv_2016: Path) -> None:
    rendered = render_csv(build_rows_2016(), list(TT_5_3_2016_NAMES))
    assert gzip.decompress(fixture_csv_2016.read_bytes()) == rendered


def test_fixture_2016_is_synthetic_with_unique_rows() -> None:
    rows = build_rows_2016()
    assert len(rows) == 26
    assert all(r["gh_project_name"].startswith("synthetic-org/") for r in rows)
    assert sorted(int(r["row"]) for r in rows) == list(range(1, 27))
    job_ids = [r["tr_job_id"] for r in rows]
    assert len(set(job_ids)) < len(job_ids)  # a job attached to a foreign build, like the release


def test_validate_task_defaults_to_primary(
    fixture_csv_2016: Path, fixture_version_2016: str
) -> None:
    out = _validate_raw_schema({"path": str(fixture_csv_2016), "version": fixture_version_2016})
    assert out["ok"] is True
    assert out["columns"] == 56
    assert out["dataset"] == TT_5_3_2016.key


def test_2017_file_fails_primary_schema(fixture_csv: Path, fixture_version: str) -> None:
    with pytest.raises(SchemaMismatchError, match="expected 56 columns, found 66"):
        _validate_raw_schema({"path": str(fixture_csv), "version": fixture_version})


def test_fingerprint_includes_pattern_overrides_only_when_present() -> None:
    plain = schema_fingerprint(TT_5_3_2016_COLUMNS)
    assert TT_5_3_2016.fingerprint() != plain  # tr_prev_build override is part of the key
    assert TT_FINAL_2017.fingerprint() == schema_fingerprint(EXPECTED_COLUMNS)
