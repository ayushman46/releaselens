"""Expected raw schema of ``final-2017-01-25.csv.gz`` and the typed cast plan.

The 66 column names (in file order) were read from the real header with DuckDB 1.5.6
(``read_csv(header=false, all_varchar=true) LIMIT 1``) on 2026-10-13 and are hard-coded here.

Raw format facts measured on the full file (3,881,992 rows) the same day:

* The null token is the literal ``NA`` (R-style); empty unquoted fields also occur. No VARCHAR
  column other than the all-``NA`` ``gh_commits_in_push`` contained the value ``NA``, so ``NA`` is
  treated as NULL in every column. (Re-checked in review: the only other null-like literal in a
  VARCHAR column is ``nil`` in ``git_branch``, 4 rows; it is kept as a string.)
* All four timestamp columns use exactly ``YYYY-MM-DD HH:MM:SS`` (no zone; UTC per the
  TravisTorrent data-format documentation). They are parsed with ``strptime`` into
  ``TIMESTAMP`` (UTC by convention, not ``TIMESTAMPTZ``).
* Booleans are the literals ``TRUE`` / ``FALSE``.
* With the types below, 0 values failed to cast in any column.

Casts are **strict** (:data:`STRICT_PATTERNS`): DuckDB's ``TRY_CAST`` alone silently rounds
``'1.5'`` to ``2`` for integer types and accepts ``'0x10'``, ``'1_000'``, ``'1e3'``, ``'nan'`` and
``'inf'``; ``strptime`` accepts unpadded ``'2014-1-1 1:2:3'``. A value must first match the
pattern for its type, otherwise it becomes NULL and counts as a cast failure in the quality gate.
Measured on the full file in review (2026-10-13): 0 non-missing values in any typed column fail
these patterns, so strict and lenient casts produce byte-identical Parquet for dataset version
``5d359345fecf0bf47be6edb10b5aae58``. That is why the patterns are not part of
:func:`schema_fingerprint` (adding them would only invalidate an identical cached conversion).
Bump ``convert.CONVERTER_VERSION`` whenever a rule change can alter the output of an existing
version.
"""

# ruff: noqa: S608 - SQL is assembled from schema constants; every value goes through sql_literal()

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import duckdb

NULL_TOKENS: tuple[str, ...] = ("NA",)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Full-match (RE2) patterns a raw value must satisfy before it is cast; see the module docstring.
# BOOLEAN is handled by an explicit TRUE/FALSE CASE and needs no pattern.
_INT_PATTERN = r"[+-]?[0-9]+"
STRICT_PATTERNS: dict[str, str] = {
    "BIGINT": _INT_PATTERN,
    "INTEGER": _INT_PATTERN,
    "DOUBLE": r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?",
    "TIMESTAMP": r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}",
}

# (name, DuckDB target type) in exact file order.
EXPECTED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tr_build_id", "BIGINT"),
    ("gh_project_name", "VARCHAR"),
    ("gh_is_pr", "BOOLEAN"),
    ("gh_pr_created_at", "TIMESTAMP"),
    ("gh_pull_req_num", "BIGINT"),
    ("gh_lang", "VARCHAR"),
    ("git_merged_with", "VARCHAR"),
    ("git_branch", "VARCHAR"),
    ("gh_num_commits_in_push", "INTEGER"),
    ("gh_commits_in_push", "VARCHAR"),
    ("git_prev_commit_resolution_status", "VARCHAR"),
    ("git_prev_built_commit", "VARCHAR"),
    ("tr_prev_build", "BIGINT"),
    ("gh_first_commit_created_at", "TIMESTAMP"),
    ("gh_team_size", "INTEGER"),
    ("git_all_built_commits", "VARCHAR"),
    ("git_num_all_built_commits", "INTEGER"),
    ("git_trigger_commit", "VARCHAR"),
    ("tr_virtual_merged_into", "VARCHAR"),
    ("gh_num_issue_comments", "INTEGER"),
    ("gh_num_commit_comments", "INTEGER"),
    ("gh_num_pr_comments", "INTEGER"),
    ("git_diff_src_churn", "BIGINT"),
    ("git_diff_test_churn", "BIGINT"),
    ("gh_diff_files_added", "INTEGER"),
    ("gh_diff_files_deleted", "INTEGER"),
    ("gh_diff_files_modified", "INTEGER"),
    ("gh_diff_tests_added", "INTEGER"),
    ("gh_diff_tests_deleted", "INTEGER"),
    ("gh_diff_src_files", "INTEGER"),
    ("gh_diff_doc_files", "INTEGER"),
    ("gh_diff_other_files", "INTEGER"),
    ("gh_num_commits_on_files_touched", "INTEGER"),
    ("gh_sloc", "BIGINT"),
    ("gh_test_lines_per_kloc", "DOUBLE"),
    ("gh_test_cases_per_kloc", "DOUBLE"),
    ("gh_asserts_cases_per_kloc", "DOUBLE"),
    ("gh_by_core_team_member", "BOOLEAN"),
    ("gh_description_complexity", "INTEGER"),
    ("gh_pushed_at", "TIMESTAMP"),
    ("gh_build_started_at", "TIMESTAMP"),
    ("gh_repo_age", "DOUBLE"),
    ("gh_repo_num_commits", "BIGINT"),
    ("tr_job_id", "BIGINT"),
    ("tr_build_number", "BIGINT"),
    ("tr_log_lan", "VARCHAR"),
    ("tr_log_status", "VARCHAR"),
    ("tr_log_setup_time", "DOUBLE"),
    ("tr_log_analyzer", "VARCHAR"),
    ("tr_log_frameworks", "VARCHAR"),
    ("tr_log_bool_tests_ran", "BOOLEAN"),
    ("tr_log_bool_tests_failed", "BOOLEAN"),
    ("tr_log_num_tests_ok", "INTEGER"),
    ("tr_log_num_tests_failed", "INTEGER"),
    ("tr_log_num_tests_run", "INTEGER"),
    ("tr_log_num_tests_skipped", "INTEGER"),
    ("tr_log_num_test_suites_run", "INTEGER"),
    ("tr_log_num_test_suites_ok", "INTEGER"),
    ("tr_log_num_test_suites_failed", "INTEGER"),
    ("tr_log_tests_failed", "VARCHAR"),
    ("tr_log_testduration", "DOUBLE"),
    ("tr_log_buildduration", "DOUBLE"),
    ("tr_original_commit", "VARCHAR"),
    ("tr_duration", "INTEGER"),
    ("tr_status", "VARCHAR"),
    ("tr_jobs", "VARCHAR"),
)

EXPECTED_NAMES: tuple[str, ...] = tuple(name for name, _ in EXPECTED_COLUMNS)
COLUMN_TYPES: dict[str, str] = dict(EXPECTED_COLUMNS)
SORT_KEY: tuple[str, ...] = ("tr_build_id", "tr_job_id")
PARTITION_COLUMN = "gh_lang"

assert len(EXPECTED_NAMES) == 66
assert len(set(EXPECTED_NAMES)) == 66


class SchemaMismatchError(ValueError):
    """Raised when the raw header differs from :data:`EXPECTED_NAMES` (names or order)."""


def schema_fingerprint() -> str:
    """sha256 of the expected schema + parsing rules; part of the conversion cache key."""
    payload = {
        "columns": [list(c) for c in EXPECTED_COLUMNS],
        "null_tokens": list(NULL_TOKENS),
        "timestamp_format": TIMESTAMP_FORMAT,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def sql_literal(value: str) -> str:
    """Single-quoted SQL string literal (DuckDB has no bind parameters for table functions)."""
    return "'" + value.replace("'", "''") + "'"


def read_header(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    """Raw header names exactly as written in the file (no DuckDB name normalisation)."""
    row = con.execute(
        f"SELECT * FROM read_csv({sql_literal(str(csv_path))}, header=false, all_varchar=true, "
        "delim=',', quote='\"', escape='\"', compression='gzip') LIMIT 1"
    ).fetchone()
    if row is None:
        raise SchemaMismatchError(f"{csv_path} is empty (no header row)")
    return ["" if v is None else str(v) for v in row]


def diff_header(actual: Sequence[str], expected: Sequence[str] = EXPECTED_NAMES) -> list[str]:
    """Human-readable differences; empty when ``actual`` equals ``expected`` exactly."""
    if list(actual) == list(expected):
        return []
    problems: list[str] = [f"expected {len(expected)} columns, found {len(actual)}"]
    missing = [c for c in expected if c not in actual]
    unexpected = [c for c in actual if c not in expected]
    dupes = sorted({c for c in actual if list(actual).count(c) > 1})
    if missing:
        problems.append(f"missing columns: {missing}")
    if unexpected:
        problems.append(f"unexpected columns: {unexpected}")
    if dupes:
        problems.append(f"duplicate columns: {dupes}")
    for i, (a, e) in enumerate(zip(actual, expected, strict=False)):
        if a != e:
            problems.append(f"first order mismatch at position {i}: expected {e!r}, found {a!r}")
            break
    udiff = list(
        difflib.unified_diff(list(expected), list(actual), "expected", "actual", lineterm="", n=1)
    )
    problems.append("diff:\n" + "\n".join(udiff[:80]))
    return problems


def validate_header(actual: Sequence[str]) -> None:
    problems = diff_header(actual)
    if problems:
        raise SchemaMismatchError("raw header mismatch: " + "; ".join(problems))
