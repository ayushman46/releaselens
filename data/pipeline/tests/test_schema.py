from __future__ import annotations

import gzip
from pathlib import Path

import duckdb
import pytest

from rl_pipeline.schema import (
    COLUMN_TYPES,
    EXPECTED_COLUMNS,
    EXPECTED_NAMES,
    SchemaMismatchError,
    diff_header,
    read_header,
    schema_fingerprint,
    validate_header,
)
from rl_pipeline.tasks import validate_raw_schema
from rl_pipeline.testing import build_rows, render_csv, write_csv_gz


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
    assert len(schema_fingerprint()) == 64


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
    problems = diff_header([*EXPECTED_NAMES, "surprise", "tr_jobs"])
    joined = "; ".join(problems)
    assert "unexpected columns: ['surprise']" in joined
    assert "duplicate columns: ['tr_jobs']" in joined


def test_exact_header_has_no_diff() -> None:
    assert diff_header(list(EXPECTED_NAMES)) == []
    validate_header(list(EXPECTED_NAMES))


def test_size_mismatch_is_rejected(fixture_csv: Path) -> None:
    with pytest.raises(Exception, match="bytes, expected"):
        validate_raw_schema({"path": str(fixture_csv), "version": "x" * 32, "size": 1})
