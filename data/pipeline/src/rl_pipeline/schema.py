"""Raw schemas of the registered TravisTorrent releases, strict cast patterns, header helpers.

Each release's column list (names in file order + DuckDB target type) was read from the real
header with DuckDB 1.5.6 (``read_csv(header=false, all_varchar=true) LIMIT 1``) and is hard-coded
here; :mod:`rl_pipeline.datasets` combines it with the source, catalog and conversion settings.

Casts are **strict** (:data:`STRICT_PATTERNS`): DuckDB's ``TRY_CAST`` alone silently rounds
``'1.5'`` to ``2`` for integer types and accepts ``'0x10'``, ``'1_000'``, ``'1e3'``, ``'nan'`` and
``'inf'``; ``strptime`` accepts unpadded ``'2014-1-1 1:2:3'``. A value must first fully match the
pattern for its type (or the column's override in the dataset spec), otherwise it becomes NULL
and counts as a cast failure in the conversion's quality gate. Bump
``convert.CONVERTER_VERSION`` whenever a rule change can alter the output of an existing version.

``final-2017-01-25.csv.gz`` (66 columns), measured on the full file (3,881,992 rows, 2026-10-13):

* The null token is the literal ``NA`` (R-style); empty unquoted fields also occur (DuckDB reads
  them as NULL). No VARCHAR column other than the all-``NA`` ``gh_commits_in_push`` contained the
  value ``NA``, so ``NA`` is treated as NULL in every column. (The only other null-like literal in
  a VARCHAR column is ``nil`` in ``git_branch``, 4 rows; it is kept as a string.)
* All four timestamp columns use exactly ``YYYY-MM-DD HH:MM:SS`` (no zone; UTC per the
  TravisTorrent data-format documentation). They are parsed with ``strptime`` into ``TIMESTAMP``.
* Booleans are the literals ``TRUE`` / ``FALSE``.
* 0 non-missing values in any typed column fail the strict patterns, so strict and lenient casts
  produce byte-identical Parquet for dataset version ``5d359345fecf0bf47be6edb10b5aae58``. That is
  why the default patterns are not part of :func:`schema_fingerprint` (adding them would only
  invalidate an identical cached conversion); per-dataset pattern *overrides* are.

``travistorrent_5_3_2016.csv.gz`` (56 columns), measured on the full file (2,640,825 job rows,
2026-10-13; see :data:`TT_5_3_2016_COLUMNS` for per-column notes):

* Same null token ``NA`` plus empty fields (``""``); every string is quoted, booleans are the quoted
  literals ``"true"`` / ``"false"``.
* ``tr_prev_build`` is written as ``"12345"`` on 1,739,408 rows and with a zero fraction
  (``"12345.0"``, ``"-1.0"``) on 901,405 rows, hence its pattern override. ``-1`` (and ``-1.0``) is
  the release's placeholder for "no previous build"; the raw layer keeps it as ``-1`` (staging
  decides). ``tr_ci_latency`` uses ``-1`` the same way.
* ``row`` is unique (1 … 2,640,825) while ``tr_job_id`` is not (1,800,390 distinct), so ``row`` is
  the conversion's unique key and the last sort column.
* 12 rows (2 Java) have shifted trailing fields: a ``tr_failed_tests`` value with unescaped quotes
  and commas spills into the following columns. Their non-parsable values become NULL and are
  counted as cast failures (well below the 0.1% gate); the rows are kept (raw stays faithful).

**SQL safety** (every module of this package): values (paths, patterns, null tokens, literals) are
always bound as DuckDB parameters (:class:`SqlParams`); identifiers (column and table names from
the fixed schemas above) cannot be bound, so they go through :func:`sql_ident`, which accepts only
plain identifiers, optionally from an allowlist, and raises otherwise.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Final

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
# An integer optionally written with an all-zero fraction ("123.0"): the value is exact, so the
# cast cannot round. Used as a per-column override where a release wrote integers as floats.
INT_WITH_ZERO_FRACTION = r"[+-]?[0-9]+(\.0+)?"
SUPPORTED_TYPES: frozenset[str] = frozenset({*STRICT_PATTERNS, "BOOLEAN", "VARCHAR"})

Columns = tuple[tuple[str, str], ...]

# ---------------------------------------------------------------- final-2017-01-25 (66 columns)
# (name, DuckDB target type) in exact file order.
FINAL_2017_COLUMNS: Columns = (
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
FINAL_2017_NAMES: tuple[str, ...] = tuple(name for name, _ in FINAL_2017_COLUMNS)

# ---------------------------------------------------------------- 5_3_2016 (56 columns)
# Types follow the 2017 schema for shared concepts (churn and sloc BIGINT, counts INTEGER, test
# densities and log-derived durations DOUBLE). Measured value shapes (full file): every integer
# column below fully matches the integer pattern on all non-missing values except the 12 shifted
# rows; ``tr_testduration`` / ``tr_purebuildduration`` mix "12", "12.0" and "12.34".
TT_5_3_2016_COLUMNS: Columns = (
    ("row", "INTEGER"),
    ("git_commit", "VARCHAR"),
    ("gh_project_name", "VARCHAR"),
    ("gh_is_pr", "BOOLEAN"),
    ("git_merged_with", "VARCHAR"),
    ("gh_lang", "VARCHAR"),
    ("git_branch", "VARCHAR"),
    ("gh_first_commit_created_at", "TIMESTAMP"),
    ("gh_team_size", "INTEGER"),
    ("git_commits", "VARCHAR"),
    ("git_num_commits", "INTEGER"),
    ("gh_num_issue_comments", "INTEGER"),
    ("gh_num_commit_comments", "INTEGER"),
    ("gh_num_pr_comments", "INTEGER"),
    ("gh_src_churn", "BIGINT"),
    ("gh_test_churn", "BIGINT"),
    ("gh_files_added", "INTEGER"),
    ("gh_files_deleted", "INTEGER"),
    ("gh_files_modified", "INTEGER"),
    ("gh_tests_added", "INTEGER"),
    ("gh_tests_deleted", "INTEGER"),
    ("gh_src_files", "INTEGER"),
    ("gh_doc_files", "INTEGER"),
    ("gh_other_files", "INTEGER"),
    ("gh_commits_on_files_touched", "INTEGER"),
    ("gh_sloc", "BIGINT"),
    ("gh_test_lines_per_kloc", "DOUBLE"),
    ("gh_test_cases_per_kloc", "DOUBLE"),
    ("gh_asserts_cases_per_kloc", "DOUBLE"),
    ("gh_by_core_team_member", "BOOLEAN"),
    ("gh_description_complexity", "INTEGER"),
    ("tr_build_id", "BIGINT"),
    ("gh_pull_req_num", "BIGINT"),
    ("tr_status", "VARCHAR"),
    ("tr_duration", "INTEGER"),
    ("tr_started_at", "TIMESTAMP"),
    ("tr_jobs", "VARCHAR"),
    ("tr_build_number", "BIGINT"),
    ("tr_job_id", "BIGINT"),
    ("tr_lan", "VARCHAR"),
    ("tr_setup_time", "DOUBLE"),
    ("tr_analyzer", "VARCHAR"),
    ("tr_frameworks", "VARCHAR"),
    ("tr_tests_ok", "INTEGER"),
    ("tr_tests_fail", "INTEGER"),
    ("tr_tests_run", "INTEGER"),
    ("tr_tests_skipped", "INTEGER"),
    ("tr_failed_tests", "VARCHAR"),
    ("tr_testduration", "DOUBLE"),
    ("tr_purebuildduration", "DOUBLE"),
    ("tr_tests_ran", "BOOLEAN"),
    ("tr_tests_failed", "BOOLEAN"),
    ("git_num_committers", "INTEGER"),
    ("tr_num_jobs", "INTEGER"),
    ("tr_prev_build", "BIGINT"),
    ("tr_ci_latency", "BIGINT"),
)
TT_5_3_2016_NAMES: tuple[str, ...] = tuple(name for name, _ in TT_5_3_2016_COLUMNS)
# Per-column pattern overrides (see the module docstring).
TT_5_3_2016_PATTERNS: dict[str, str] = {"tr_prev_build": INT_WITH_ZERO_FRACTION}

for _cols, _n in ((FINAL_2017_COLUMNS, 66), (TT_5_3_2016_COLUMNS, 56)):
    assert len(_cols) == _n
    assert len({c for c, _ in _cols}) == _n
    assert {t for _, t in _cols} <= SUPPORTED_TYPES


class SchemaMismatchError(ValueError):
    """Raised when the raw header differs from the expected names (names or order)."""


def schema_fingerprint(
    columns: Columns,
    null_tokens: Sequence[str] = NULL_TOKENS,
    timestamp_format: str = TIMESTAMP_FORMAT,
    patterns: Mapping[str, str] | None = None,
) -> str:
    """sha256 of the expected schema + parsing rules; part of the conversion cache key.

    Pattern *overrides* are included only when present, so a dataset without overrides keeps the
    fingerprint it had before overrides existed (and its cached conversion stays valid).
    """
    payload: dict[str, object] = {
        "columns": [list(c) for c in columns],
        "null_tokens": list(null_tokens),
        "timestamp_format": timestamp_format,
    }
    if patterns:
        payload["patterns"] = dict(sorted(patterns.items()))
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


_IDENT_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def sql_ident(name: str, allowed: Collection[str] | None = None) -> str:
    """``name`` as a double-quoted SQL identifier, after validating it.

    Identifiers cannot be bound as parameters, so every statement that interpolates a column or
    table name validates it here: it must be a plain identifier (``[A-Za-z_][A-Za-z0-9_]*``, which
    every column of the registered schemas is) and, when ``allowed`` is given, one of those names.
    Anything else raises :class:`ValueError` before a statement is built.
    """
    if not _IDENT_RE.fullmatch(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"SQL identifier {name!r} is not one of the expected names")
    return f'"{name}"'


class SqlParams:
    """Named DuckDB parameters (``$p0``, ``$p1``, ...) collected while one statement is assembled.

    ``params(value)`` returns the placeholder for ``value`` (equal values share one placeholder);
    keyword arguments pre-register named parameters (``SqlParams(csv=path)`` -> ``$csv``). Execute
    the statement with ``con.execute(sql, params.values)``: DuckDB rejects unused parameters, so use
    one instance per statement.
    """

    __slots__ = ("values",)

    def __init__(self, **named: object) -> None:
        for key in named:
            if not _IDENT_RE.fullmatch(key) or re.fullmatch(r"p[0-9]+", key):
                raise ValueError(f"invalid parameter name {key!r}")
        self.values: dict[str, object] = dict(named)

    def __call__(self, value: object) -> str:
        for key, existing in self.values.items():
            if type(existing) is type(value) and existing == value:
                return f"${key}"
        key = f"p{len(self.values)}"
        self.values[key] = value
        return f"${key}"


def read_header(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    """Raw header names exactly as written in the file (no DuckDB name normalisation)."""
    row = con.execute(
        "SELECT * FROM read_csv($csv, header=false, all_varchar=true, "
        "delim=',', quote='\"', escape='\"', compression='gzip') LIMIT 1",
        {"csv": str(csv_path)},
    ).fetchone()
    if row is None:
        raise SchemaMismatchError(f"{csv_path} is empty (no header row)")
    return ["" if v is None else str(v) for v in row]


def diff_header(actual: Sequence[str], expected: Sequence[str]) -> list[str]:
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


def validate_header(actual: Sequence[str], expected: Sequence[str]) -> None:
    problems = diff_header(actual, expected)
    if problems:
        raise SchemaMismatchError("raw header mismatch: " + "; ".join(problems))
