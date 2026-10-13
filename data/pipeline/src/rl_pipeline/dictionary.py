"""Generate a dataset's data dictionary (Markdown + JSON twin) from its converted Parquet.

Every number is computed from the data at build time; meanings and availability tags come from the
dataset spec's catalog (:mod:`rl_pipeline.catalog`, :mod:`rl_pipeline.catalog_2016`).
Measurements use the **Java subset** (``gh_lang = 'java'``):

* build-grain columns: one row per ``tr_build_id``, chosen deterministically (2017: the row with
  the lowest ``tr_job_id``; 2016: a row whose ``tr_job_id`` is listed in the build's own
  ``tr_jobs`` first, then the lowest ``(tr_job_id, row)``, because that release attaches some job
  rows to foreign builds);
* job-grain columns: every job row (raw is not deduplicated).

No raw values of free-text columns are written. Numeric/timestamp columns get min/median/max,
booleans their TRUE share; the only categorical values shown are ``gh_lang`` and the
``tr_status`` label distribution. Output is deterministic (sorted keys, no generation clock).

Optional: ``crosscheck_dir`` = the monperrus ``travistorrent-java-ci-build-dataset`` archive
(``*/repo-data-travis.csv`` with explicit-UTC ``started_at``). The builder then joins it on the
build id and reports how many start times, statuses and durations agree (the UTC check).

SQL safety: file paths and literals are bound parameters (or DuckDB relations); column names come
from the dataset spec and pass :func:`rl_pipeline.schema.sql_ident` before they reach a statement.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from rl_pipeline.convert import PART_FILENAME, DuckDBLimits, connect, read_manifest, temp_budget
from rl_pipeline.datasets import PRIMARY, SECONDARY, DatasetSpec
from rl_pipeline.schema import sql_ident

log = logging.getLogger("rl_pipeline.dictionary")

JAVA = "java"
_NUMERIC = {"BIGINT", "INTEGER", "DOUBLE"}
MOSTLY_ZERO_THRESHOLD = 0.95
_IN_OWN = "__in_own_jobs"
_TABLES = frozenset({"java_jobs", "java_builds"})
_SHORT = {PRIMARY: "2016", SECONDARY: "2017"}
_ARCHIVE_GLOB = "*/repo-data-travis.csv"
_UTC_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC"


def _q(name: str) -> str:
    """A validated, quoted identifier (schema column names and the helper column only)."""
    return sql_ident(name)


def _num(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float):
        return round(value, 4)
    return value


def _one(
    con: duckdb.DuckDBPyConnection, sql: str, params: dict[str, object] | None = None
) -> tuple[Any, ...]:
    row = con.execute(sql, params).fetchone()
    assert row is not None
    return row


def _jobs_list(spec: DatasetSpec, col: str = "tr_jobs") -> str:
    """SQL: the build's job-id list as BIGINT[] (NULLs dropped)."""
    if spec.jobs_list_format == "bracket":
        parts = f"string_split(trim({_q(col)}, '[] '), ',')"
    else:
        parts = f"string_split({_q(col)}, '#')"
    ids = f"list_transform({parts}, x -> TRY_CAST(trim(x) AS BIGINT))"
    return f"list_filter({ids}, x -> x IS NOT NULL)"


def _prefers_listed_job(spec: DatasetSpec) -> bool:
    # Only releases whose job ids are not unique attach job rows to foreign builds.
    return spec.unique_column != spec.job_id_column


def _build_rule(spec: DatasetSpec) -> str:
    if _prefers_listed_job(spec):
        return (
            f"one row per {spec.build_id_column}: a job row whose {spec.job_id_column} is listed "
            f"in the build's own tr_jobs first, then the lowest ({spec.job_id_column}, "
            f"{spec.unique_column})"
        )
    return f"one row per {spec.build_id_column} (the job row with the lowest {spec.job_id_column})"


def _setup_views(con: duckdb.DuckDBPyConnection, local_dir: Path, spec: DatasetSpec) -> None:
    glob = str(local_dir / "lang=*" / PART_FILENAME)
    java_file = local_dir / f"lang={JAVA}" / PART_FILENAME
    if not java_file.is_file():
        raise FileNotFoundError(f"{java_file} missing: the dictionary is measured on Java")
    # Views cannot take prepared parameters: the Parquet sources (paths) and the Java filter (a
    # literal) are DuckDB relations, so no value is spliced into SQL text.
    con.read_parquet(glob, hive_partitioning=False).create_view("jobs", replace=True)
    java_src = con.read_parquet(str(java_file), hive_partitioning=False).filter(
        duckdb.ColumnExpression(spec.partition_column) == duckdb.ConstantExpression(JAVA)
    )
    java_src.create_view("java_src", replace=True)
    job = _q(spec.job_id_column)
    in_own = _q(_IN_OWN)
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW java_jobs AS SELECT *, "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
        f"coalesce(list_contains({_jobs_list(spec)}, {job}), false) AS {in_own} FROM java_src"
    )
    order = f"{job}"
    if _prefers_listed_job(spec):
        order = f"(NOT {in_own}), {job}, {_q(spec.unique_column)}"
    con.execute(
        "CREATE OR REPLACE TEMP TABLE java_builds AS SELECT * FROM java_jobs "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
        f"QUALIFY row_number() OVER (PARTITION BY {_q(spec.build_id_column)} ORDER BY {order}) = 1"
    )


def _counts(con: duckdb.DuckDBPyConnection, spec: DatasetSpec) -> dict[str, Any]:
    job, build = _q(spec.job_id_column), _q(spec.build_id_column)
    agg = (
        f"count(*), count(DISTINCT {job}), count(DISTINCT {build}), count(DISTINCT gh_project_name)"
    )
    total = _one(con, f"SELECT {agg} FROM jobs")  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
    by_lang = con.execute(
        f"SELECT {_q(spec.partition_column)}, {agg} FROM jobs GROUP BY 1 ORDER BY 1"  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
    ).fetchall()
    java = _one(con, f"SELECT {agg} FROM java_jobs")  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
    started = _q(spec.started_at_column)
    rng = _one(con, f"SELECT min({started}), max({started}) FROM java_builds")  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
    label = _q(spec.label_column)
    labels = con.execute(
        f"SELECT {label}, count(*) FROM java_builds GROUP BY 1 ORDER BY 2 DESC, 1"  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
    ).fetchall()
    keys = ("jobs", "distinct_job_ids", "builds", "projects")
    return {
        "overall": dict(zip(keys, map(int, total), strict=True)),
        "by_lang": {str(r[0]): dict(zip(keys, map(int, r[1:]), strict=True)) for r in by_lang},
        "java": dict(zip(keys, map(int, java), strict=True)),
        "java_build_started_at_range": {
            "column": spec.started_at_column,
            "min": _num(rng[0]),
            "max": _num(rng[1]),
        },
        "java_label_distribution": {
            ("<null>" if r[0] is None else str(r[0])): int(r[1]) for r in labels
        },
    }


def _varying_builds(
    con: duckdb.DuckDBPyConnection, spec: DatasetSpec, chunk: int = 8
) -> dict[str, int]:
    """Per column: Java builds whose jobs disagree (different values, or NULL mixed with value).

    ``min <> max`` instead of ``count(DISTINCT) > 1`` keeps per-group state constant-size.
    """
    out: dict[str, int] = {}
    names = list(spec.names)
    for i in range(0, len(names), chunk):
        part = names[i : i + chunk]
        flags = ", ".join(
            f"(coalesce(min({_q(n)}) <> max({_q(n)}), false) OR "
            f"(count({_q(n)}) > 0 AND count({_q(n)}) < count(*)))::INTEGER AS {_q(n)}"
            for n in part
        )
        sums = ", ".join(f"sum({_q(n)})" for n in part)
        row = _one(
            con,
            f"SELECT {sums} FROM (SELECT {flags} FROM java_jobs "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
            f"GROUP BY {_q(spec.build_id_column)})",
        )
        out.update({n: int(v or 0) for n, v in zip(part, row, strict=True)})
    return out


def _column_profile(
    con: duckdb.DuckDBPyConnection, spec: DatasetSpec, name: str, duck_type: str
) -> dict[str, Any]:
    doc = spec.column_docs[name]
    table = sql_ident("java_jobs" if doc.grain == "job" else "java_builds", _TABLES)
    col = _q(name)
    exprs = ["count(*)", f"count(*) - count({col})", f"count(DISTINCT {col})"]
    with_stats = duck_type in _NUMERIC | {"TIMESTAMP"} and doc.role != "identifier"
    if with_stats:
        median = f"median({col})" if duck_type in _NUMERIC else "NULL"
        exprs += [f"min({col})", median, f"max({col})"]
    if duck_type == "BOOLEAN":
        exprs.append(f"avg({col}::INTEGER)")
    with_zero = duck_type in _NUMERIC and doc.role != "identifier"
    if with_zero:
        exprs.append(f"avg(({col} = 0)::INTEGER)")
    row = _one(con, f"SELECT {', '.join(exprs)} FROM {table}")  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
    n, nulls, distinct = int(row[0]), int(row[1]), int(row[2])
    out: dict[str, Any] = {
        "rows_measured": n,
        "null_count": nulls,
        "null_rate": round(nulls / n, 6) if n else None,
        "distinct_count": distinct,
    }
    if with_stats:
        out["min"], out["median"], out["max"] = _num(row[3]), _num(row[4]), _num(row[5])
        if duck_type == "TIMESTAMP":
            out.pop("median")
    if duck_type == "BOOLEAN":
        out["true_rate"] = None if row[3] is None else round(float(row[3]), 6)
    if with_zero:
        out["zero_rate"] = None if row[-1] is None else round(float(row[-1]), 6)
    return out


def _all_null(profile: dict[str, Any]) -> bool:
    """Exact (counts), not ``null_rate == 1.0``: the rate is rounded to 6 decimals."""
    if "null_count" in profile and "rows_measured" in profile:
        return bool(profile["rows_measured"]) and profile["null_count"] == profile["rows_measured"]
    return profile.get("null_rate") == 1.0


def quality_flags(profile: dict[str, Any], role: str) -> list[str]:
    """Measured red flags for a column (Java subset). Identifiers/context are not flagged."""
    flags: list[str] = []
    if _all_null(profile):
        flags.append("all_null")
    elif profile["distinct_count"] == 1 and role not in {"identifier", "context"}:
        flags.append("constant")
    zero = profile.get("zero_rate")
    if zero is not None and zero >= MOSTLY_ZERO_THRESHOLD and "constant" not in flags:
        flags.append("mostly_zero")
    return flags


def _rate(part: Any, whole: Any) -> float | None:
    return round(int(part) / int(whole), 6) if whole else None


def _inv(rule: str, violations: Any, of: Any, of_label: str, note: str = "") -> dict[str, Any]:
    return {
        "rule": rule,
        "violations": int(violations or 0),
        "of": int(of or 0),
        "of_label": of_label,
        "note": note,
    }


# ---------------------------------------------------------------- final-2017 specifics
def _evidence_final_2017(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Measured behaviour of the two undocumented repository columns (Java builds)."""
    age = _one(
        con,
        "SELECT median(sl), quantile_cont(sl, 0.1), quantile_cont(sl, 0.9), count(*) FROM ("
        "SELECT regr_slope(gh_repo_age, epoch(gh_build_started_at) / 86400.0) AS sl "
        "FROM java_builds GROUP BY gh_project_name HAVING count(*) >= 20) WHERE sl IS NOT NULL",
    )
    commits = _one(
        con,
        "SELECT avg((cur >= prev)::INTEGER), count(*) FROM ("
        "SELECT gh_repo_num_commits AS cur, lag(gh_repo_num_commits) OVER ("
        "PARTITION BY gh_project_name ORDER BY gh_build_started_at, tr_build_id) AS prev "
        "FROM java_builds) WHERE prev IS NOT NULL AND cur IS NOT NULL",
    )
    varying = _one(
        con,
        "SELECT count(*) FILTER (WHERE d > 1), count(*) FROM (SELECT "
        "count(DISTINCT gh_repo_num_commits) AS d FROM java_builds GROUP BY gh_project_name)",
    )
    master = _one(
        con,
        "SELECT avg((cur >= prev)::INTEGER), avg((cur > prev)::INTEGER), count(*) FROM ("
        "SELECT gh_repo_num_commits AS cur, lag(gh_repo_num_commits) OVER ("
        "PARTITION BY gh_project_name ORDER BY gh_build_started_at, tr_build_id) AS prev "
        "FROM java_builds WHERE git_branch = 'master' AND NOT gh_is_pr) "
        "WHERE prev IS NOT NULL AND cur IS NOT NULL",
    )
    commit_slope = _one(
        con,
        "SELECT count(*) FILTER (WHERE sl > 0), count(*) FROM ("
        "SELECT regr_slope(gh_repo_num_commits, epoch(gh_build_started_at) / 86400.0) AS sl "
        "FROM java_builds GROUP BY gh_project_name HAVING count(*) >= 20) WHERE sl IS NOT NULL",
    )
    return {
        "gh_repo_age": {
            "method": "per-project OLS slope of gh_repo_age on days since epoch of "
            "gh_build_started_at (Java projects with >= 20 builds)",
            "median_slope_days_per_day": _num(age[0]),
            "p10_slope": _num(age[1]),
            "p90_slope": _num(age[2]),
            "projects": int(age[3]),
        },
        "gh_repo_num_commits": {
            "method": "share of consecutive builds of a project (ordered by "
            "gh_build_started_at) whose value does not decrease",
            "non_decreasing_share": _num(commits[0]),
            "pairs": int(commits[1]),
            "projects_with_more_than_one_value": int(varying[0]),
            "projects": int(varying[1]),
            "master_push_builds": {
                "method": "same, restricted to non-PR builds on git_branch = 'master'",
                "non_decreasing_share": _num(master[0]),
                "strictly_increasing_share": _num(master[1]),
                "pairs": int(master[2]),
            },
            "positive_time_slope_projects": int(commit_slope[0]),
            "slope_projects": int(commit_slope[1]),
        },
    }


def _invariants_final_2017(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Measured violations of ordering facts that leakage-safe features rely on (Java builds)."""
    pr = _one(
        con,
        "SELECT count(*) FILTER (WHERE gh_pr_created_at > gh_build_started_at), "
        "count(*) FILTER (WHERE gh_is_pr), "
        "max(epoch(gh_pr_created_at) - epoch(gh_build_started_at)) FILTER ("
        "WHERE gh_pr_created_at > gh_build_started_at) / 3600.0 FROM java_builds",
    )
    prev = _one(
        con,
        "SELECT count(*) FILTER (WHERE tr_prev_build >= tr_build_id), count(tr_prev_build) "
        "FROM java_builds",
    )
    hours = _num(None if pr[2] is None else float(pr[2]))
    return {
        "pr_created_after_build_start": _inv(
            "gh_pr_created_at <= gh_build_started_at (a PR build needs an open PR)",
            pr[0],
            pr[1],
            "PR builds",
            f"up to {hours} h after the build started" if pr[0] else "",
        ),
        "prev_build_not_earlier": _inv(
            "tr_prev_build < tr_build_id (the previous build was created earlier)",
            prev[0],
            prev[1],
            "builds with a previous build",
        ),
    }


# ---------------------------------------------------------------- 5_3_2016 specifics
def _evidence_5_3_2016(con: duckdb.DuckDBPyConnection, spec: DatasetSpec) -> dict[str, Any]:
    """Measured facts that decide between documentation versions (Java subset)."""
    fc = _one(
        con,
        "SELECT count(*), "
        "count(*) FILTER (WHERE gh_first_commit_created_at = "
        "timezone('Europe/London', timezone('UTC', tr_started_at))), "
        "count(*) FILTER (WHERE epoch(gh_first_commit_created_at) - epoch(tr_started_at) = 0), "
        "count(*) FILTER (WHERE epoch(gh_first_commit_created_at) - epoch(tr_started_at) = 3600),"
        " count(*) FILTER (WHERE epoch(gh_first_commit_created_at) - epoch(tr_started_at) = 7200)"
        " FROM java_jobs WHERE gh_first_commit_created_at IS NOT NULL "
        "AND tr_started_at IS NOT NULL",
    )
    projects_const = _one(
        con,
        "SELECT count(*) FILTER (WHERE d = 1), count(*) FROM (SELECT gh_project_name, "
        "count(DISTINCT gh_first_commit_created_at) AS d FROM java_builds GROUP BY 1)",
    )
    job, build = _q(spec.job_id_column), _q(spec.build_id_column)
    dups = _one(
        con,
        f"SELECT count(*), count(*) FILTER (WHERE n > 1), count(*) FILTER (WHERE nb > 1), "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
        f"count(*) FILTER (WHERE n > 1 AND k = 1), count(*) FILTER (WHERE n > 1 AND k = 0), "
        f"sum(n) FILTER (WHERE n > 1) FROM (SELECT {job}, count(*) AS n, "
        f"count(DISTINCT {build}) AS nb, count(*) FILTER (WHERE {_IN_OWN}) AS k "
        f"FROM java_jobs GROUP BY {job})",
    )
    listed = _one(
        con,
        f"SELECT count(*) FILTER (WHERE {_IN_OWN}), count(*), "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
        f"(SELECT count(*) FROM (SELECT {build} FROM java_jobs GROUP BY 1 "
        f"HAVING NOT bool_or({_IN_OWN}))) FROM java_jobs",
    )
    tests = _one(
        con,
        "SELECT count(*) FILTER (WHERE gh_test_churn > 0), "
        "count(*) FILTER (WHERE gh_test_churn > 0 AND gh_tests_added = 0 "
        "AND gh_tests_deleted = 0), "
        "count(*) FILTER (WHERE gh_tests_added > gh_test_churn), "
        "count(*) FILTER (WHERE gh_tests_added IS NOT NULL AND gh_test_churn IS NOT NULL) "
        "FROM java_builds",
    )
    files = _one(
        con,
        "SELECT count(*) FILTER (WHERE gh_src_files + gh_doc_files + gh_other_files <= "
        "gh_files_added + gh_files_deleted + gh_files_modified), "
        "count(*) FILTER (WHERE gh_src_files + gh_doc_files + gh_other_files = "
        "gh_files_added + gh_files_deleted + gh_files_modified), "
        "count(gh_src_files + gh_doc_files + gh_other_files + gh_files_added + gh_files_deleted "
        "+ gh_files_modified) FROM java_builds",
    )
    commits = _one(
        con,
        "SELECT count(*) FILTER (WHERE git_num_commits = len(string_split(git_commits, '#'))), "
        "count(*) FILTER (WHERE contains(git_commits, git_commit)), "
        "min(git_num_commits), count(git_num_commits) FROM java_jobs",
    )
    committers = _one(
        con,
        "SELECT count(*) FILTER (WHERE git_num_committers <= git_num_commits + 1), "
        "count(*) FILTER (WHERE git_num_committers = 1), "
        "count(*) FILTER (WHERE git_num_committers IS NOT NULL AND git_num_commits IS NOT NULL) "
        "FROM java_jobs",
    )
    return {
        "gh_first_commit_created_at": {
            "method": "Java job rows: gh_first_commit_created_at compared with tr_started_at",
            "rows": int(fc[0]),
            "equal_to_started_at_in_europe_london": int(fc[1]),
            "offset_0h": int(fc[2]),
            "offset_plus_1h": int(fc[3]),
            "offset_plus_2h": int(fc[4]),
            "projects_with_a_single_value": int(projects_const[0]),
            "projects": int(projects_const[1]),
        },
        "duplicate_job_rows": {
            "method": "Java job rows grouped by tr_job_id; 'listed' = the job id appears in the "
            "row's own tr_jobs",
            "distinct_job_ids": int(dups[0]),
            "job_ids_on_more_than_one_row": int(dups[1]),
            "job_ids_in_more_than_one_build": int(dups[2]),
            "duplicated_ids_listed_on_exactly_one_row": int(dups[3]),
            "duplicated_ids_listed_on_no_row": int(dups[4]),
            "rows_of_duplicated_ids": int(dups[5] or 0),
            "rows_listed_in_own_build": int(listed[0]),
            "rows": int(listed[1]),
            "builds_without_a_listed_row": int(listed[2]),
        },
        "tests_added_definition": {
            "method": "Java builds: if gh_tests_added/deleted were lines of test code, test churn "
            "> 0 would imply tests added or deleted > 0, and tests added could not exceed churn",
            "builds_with_test_churn": int(tests[0]),
            "with_test_churn_but_no_tests_added_or_deleted": int(tests[1]),
            "tests_added_greater_than_test_churn": int(tests[2]),
            "builds_measured": int(tests[3]),
        },
        "file_count_definition": {
            "method": "Java builds: src + doc + other files vs files added + deleted + modified "
            "(repository-wide counts would usually exceed the touched files)",
            "categorised_le_touched": int(files[0]),
            "categorised_eq_touched": int(files[1]),
            "builds_measured": int(files[2]),
        },
        "git_commits": {
            "method": "Java job rows",
            "num_commits_equals_entries": int(commits[0]),
            "git_commits_contains_git_commit": int(commits[1]),
            "min_git_num_commits": _num(commits[2]),
            "rows_measured": int(commits[3]),
        },
        "git_num_committers": {
            "method": "Java job rows with both values",
            "at_most_num_commits_plus_1": int(committers[0]),
            "equal_to_1": int(committers[1]),
            "rows_measured": int(committers[2]),
        },
    }


def _invariants_5_3_2016(
    con: duckdb.DuckDBPyConnection, spec: DatasetSpec, varying: dict[str, int]
) -> dict[str, Any]:
    job, build = _q(spec.job_id_column), _q(spec.build_id_column)
    rows = _one(
        con,
        f"SELECT count(*), count(DISTINCT {job}), count(*) FILTER (WHERE NOT {_IN_OWN}), "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
        f"count(*) FILTER (WHERE tr_num_jobs <> len({_jobs_list(spec)})), "
        "count(*) FILTER (WHERE tr_num_jobs IS NOT NULL AND tr_jobs IS NOT NULL), "
        "count(*) FILTER (WHERE tr_prev_build = -1), "
        f"count(*) FILTER (WHERE tr_prev_build > 0 AND tr_prev_build >= {build}), "
        "count(*) FILTER (WHERE tr_prev_build > 0), count(tr_prev_build), "
        "count(*) FILTER (WHERE tr_prev_build < -1 OR tr_prev_build = 0), "
        "count(*) FILTER (WHERE least(tr_tests_ok, tr_tests_fail, tr_tests_run, "
        "tr_tests_skipped) < 0), "
        "count(*) FILTER (WHERE coalesce(tr_tests_ok, tr_tests_fail, tr_tests_run, "
        "tr_tests_skipped) IS NOT NULL), "
        "min(least(tr_tests_ok, tr_tests_fail, tr_tests_run, tr_tests_skipped)) FROM java_jobs",
    )
    b = _one(
        con,
        "SELECT count(*), count(*) FILTER (WHERE tr_ci_latency = -1), "
        "count(*) FILTER (WHERE tr_ci_latency < -1), count(tr_ci_latency), "
        "min(tr_ci_latency), "
        "count(*) FILTER (WHERE gh_first_commit_created_at > tr_started_at), "
        "count(*) FILTER (WHERE gh_first_commit_created_at IS NOT NULL "
        "AND tr_started_at IS NOT NULL), "
        "count(*) FILTER (WHERE gh_first_commit_created_at > tr_started_at AND "
        "epoch(gh_first_commit_created_at) - epoch(tr_started_at) NOT IN (3600, 7200)) "
        "FROM java_builds",
    )
    build_cols = [
        n for n in spec.names if spec.column_docs[n].grain == "build" and varying.get(n, 0) > 0
    ]
    worst = max((varying[n] for n in build_cols), default=0)
    return {
        "job_id_unique": _inv(
            "tr_job_id is unique across job rows",
            int(rows[0]) - int(rows[1]),
            rows[0],
            "job rows",
            "extra rows beyond one per distinct tr_job_id (see evidence: duplicate job rows)",
        ),
        "job_listed_in_own_build": _inv(
            "tr_job_id is one of its build's tr_jobs",
            rows[2],
            rows[0],
            "job rows",
        ),
        "num_jobs_matches_list": _inv(
            "tr_num_jobs = length of tr_jobs",
            rows[3],
            rows[4],
            "job rows with both values",
        ),
        "build_columns_constant": _inv(
            "build-grain columns are constant within a build",
            len(build_cols),
            sum(1 for n in spec.names if spec.column_docs[n].grain == "build"),
            "build-grain columns",
            (f"varying: {', '.join(build_cols)} (at most {worst:,} builds)" if build_cols else ""),
        ),
        "prev_build_placeholder": _inv(
            "tr_prev_build is a build id (not the -1 placeholder)",
            rows[5],
            rows[8],
            "job rows with a value",
        ),
        "prev_build_other_non_positive": _inv(
            "tr_prev_build is > 0 unless it is the -1 placeholder",
            rows[9],
            rows[8],
            "job rows with a value",
        ),
        "prev_build_not_earlier": _inv(
            "tr_prev_build < tr_build_id (the previous build was created earlier)",
            rows[6],
            rows[7],
            "job rows with tr_prev_build > 0",
            "order history by tr_started_at with the finished-before rule, not by this link"
            if rows[6]
            else "",
        ),
        "test_counts_non_negative": _inv(
            "tr_tests_ok / _fail / _run / _skipped >= 0",
            rows[10],
            rows[11],
            "job rows with a test count",
            f"minimum {rows[12]}" if rows[10] else "",
        ),
        "ci_latency_placeholder": _inv(
            "tr_ci_latency is not the -1 placeholder",
            b[1],
            b[3],
            "builds with a value",
        ),
        "ci_latency_non_negative": _inv(
            "tr_ci_latency >= 0 unless it is the -1 placeholder",
            b[2],
            b[3],
            "builds with a value",
            f"minimum {b[4]} s" if b[2] else "",
        ),
        "first_commit_before_start": _inv(
            "gh_first_commit_created_at <= tr_started_at (the first commit precedes the build)",
            b[5],
            b[6],
            "builds",
            (
                "every violation is an exact +1 h / +2 h offset (see evidence)"
                if b[5] and not b[7]
                else (f"{b[7]:,} violations are not a +1 h / +2 h offset" if b[7] else "")
            ),
        ),
    }


def _cast_failures(manifest: dict[str, Any]) -> dict[str, Any]:
    cols = manifest.get("cast_profile", {}).get("columns", {})
    failing = {
        name: {"cast_failures": c["cast_failures"], "non_missing": c["non_missing"]}
        for name, c in cols.items()
        if c.get("cast_failures")
    }
    return {
        "scope": "all languages, every job row, per the conversion manifest",
        "max_failure_rate": manifest.get("cast_profile", {}).get("max_failure_rate"),
        "columns": failing,
    }


def utc_crosscheck(
    con: duckdb.DuckDBPyConnection, spec: DatasetSpec, archive_dir: Path
) -> dict[str, Any]:
    """Join Java builds with the monperrus archive's explicit-UTC ``started_at`` per build id."""
    files = sorted(archive_dir.glob(_ARCHIVE_GLOB))
    if not files:
        raise FileNotFoundError(f"no {_ARCHIVE_GLOB} under {archive_dir}")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE archive_raw AS SELECT build_id, status, duration, "
        "started_at FROM read_csv($files, header=true, all_varchar=true, union_by_name=true)",
        {"files": [str(f) for f in files]},
    )
    raw = _one(
        con,
        "SELECT count(*), count(*) FILTER (WHERE regexp_full_match(started_at, $utc)) "
        "FROM archive_raw",
        {"utc": _UTC_PATTERN},
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE archive AS SELECT DISTINCT TRY_CAST(build_id AS BIGINT) AS b,"
        " status, TRY_CAST(duration AS BIGINT) AS dur, "
        "strptime(replace(started_at, ' UTC', ''), '%Y-%m-%d %H:%M:%S') AS st FROM archive_raw "
        "WHERE regexp_full_match(started_at, $utc)",
        {"utc": _UTC_PATTERN},
    )
    started, build = _q(spec.started_at_column), _q(spec.build_id_column)
    label, duration = _q(spec.label_column), _q(spec.duration_column)
    joined = _one(
        con,
        f"SELECT count(*), count(*) FILTER (WHERE j.{started} = a.st), "  # noqa: S608  # nosec B608 # identifiers: sql_ident (schema names); no values spliced
        f"count(*) FILTER (WHERE abs(epoch(j.{started}) - epoch(a.st)) IN (3600, 7200)), "
        f"count(*) FILTER (WHERE j.{label} = a.status), "
        f"count(*) FILTER (WHERE j.{duration} = a.dur), count(DISTINCT j.gh_project_name) "
        f"FROM java_builds j JOIN archive a ON j.{build} = a.b",
    )
    return {
        "archive": archive_dir.name,
        "archive_files": len(files),
        "archive_rows": int(raw[0]),
        "archive_rows_with_explicit_utc": int(raw[1]),
        "joined_java_builds": int(joined[0]),
        "joined_projects": int(joined[5]),
        "started_at_equal": int(joined[1]),
        "started_at_off_by_1h_or_2h": int(joined[2]),
        "status_equal": int(joined[3]),
        "duration_equal": int(joined[4]),
        "started_at_column": spec.started_at_column,
    }


def _extras(
    con: duckdb.DuckDBPyConnection, spec: DatasetSpec, varying: dict[str, int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(evidence, invariants) for the spec's release."""
    if spec.key == SECONDARY:
        return _evidence_final_2017(con), _invariants_final_2017(con)
    if spec.key == PRIMARY:
        return _evidence_5_3_2016(con, spec), _invariants_5_3_2016(con, spec, varying)
    return {}, {}


# ---------------------------------------------------------------- build
def build_dictionary(
    local_dir: Path,
    spec: DatasetSpec,
    limits: DuckDBLimits | None = None,
    *,
    crosscheck_dir: Path | None = None,
) -> dict[str, Any]:
    manifest = read_manifest(local_dir)
    if manifest is None:
        raise FileNotFoundError(f"{local_dir}/_manifest.json missing: convert first")
    schema = [(str(c["name"]), str(c["type"])) for c in manifest.get("schema", [])]
    if schema != list(spec.columns):
        raise ValueError(f"{local_dir} was not converted with the {spec.key!r} schema")
    limits = limits or DuckDBLimits()
    tmp_dir = local_dir.parent / f".duckdb_tmp-dict-{os.getpid()}"
    con = connect(limits, tmp_dir, max_temp_bytes=temp_budget(local_dir.parent, 0, limits))
    try:
        _setup_views(con, local_dir, spec)
        counts = _counts(con, spec)
        varying = _varying_builds(con, spec)
        columns: list[dict[str, Any]] = []
        for pos, (name, duck_type) in enumerate(spec.columns, start=1):
            doc = spec.column_docs[name]
            entry: dict[str, Any] = {
                "position": pos,
                "name": name,
                "duckdb_type": duck_type,
                "meaning": doc.meaning,
                "meaning_source": "official data-format page" if doc.documented else "none",
                "grain": doc.grain,
                "role": doc.role,
                "availability": doc.availability,
                "justification": doc.justification,
                "model_input_default": doc.role == "feature_candidate",
                "java_builds_with_varying_values": varying[name],
            }
            entry["java_profile"] = _column_profile(con, spec, name, duck_type)
            entry["quality_flags"] = quality_flags(entry["java_profile"], doc.role)
            # A column with no value or a single value can never be a useful model input.
            if {"all_null", "constant"} & set(entry["quality_flags"]):
                entry["model_input_default"] = False
            columns.append(entry)
        evidence, invariants = _extras(con, spec, varying)
        crosscheck = utc_crosscheck(con, spec, crosscheck_dir) if crosscheck_dir else None
    finally:
        con.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    header = set(spec.names)
    by_name = {c["name"]: c for c in columns}
    source = manifest.get("source") or {}
    return {
        "title": f"{spec.title} data dictionary",
        "generator": "rl_pipeline.dictionary (python -m rl_pipeline build_data_dictionary "
        f"--dataset {spec.key})",
        "dataset": {
            "key": spec.key,
            "title": spec.title,
            "version": manifest["version"],
            "md5": source.get("md5", manifest["version"]),
            "filename": source.get("filename", spec.source.filename),
            "size_bytes": source.get("size", spec.source.size),
            "doi": spec.source.doi,
            "url": spec.source.url,
            "license": spec.source.license,
            "attribution": spec.source.citation,
            "timestamp_semantics": spec.timestamp_semantics,
        },
        "dataformat_doc": {
            "url": spec.dataformat.url,
            "commit": spec.dataformat.commit,
            "sha256": spec.dataformat.sha256,
            "note": spec.dataformat.note,
        },
        "measurement": {
            "subset": f"{spec.partition_column} = '{JAVA}'",
            "build_grain": _build_rule(spec),
            "job_grain": "every job row (raw is not deduplicated)",
            "distinct_count": "exact count(DISTINCT), NULL excluded",
            "null_definition": "SQL NULL after conversion (raw empty field, the 'NA' token, or a "
            "value that failed its strict cast — see Cast failures)",
            "source_files": [f["path"] for f in manifest["files"]],
        },
        "counts": counts,
        "spec_name_map": [
            {
                "spec_name": spec_name,
                "column": real,
                "renamed": spec_name != real,
                "present_in_header": real in header,
                "availability": spec.column_docs[real].availability if real in header else None,
            }
            for spec_name, real in spec.spec_name_map.items()
        ],
        "build_input_map": [
            {
                "field": m.field,
                "column": m.column,
                "status": m.status,
                "note": m.note,
                "availability": spec.column_docs[m.column].availability if m.column else None,
                "java_null_rate": by_name[m.column]["java_profile"]["null_rate"]
                if m.column
                else None,
                "java_zero_rate": by_name[m.column]["java_profile"].get("zero_rate")
                if m.column
                else None,
            }
            for m in spec.build_input_map
        ],
        "undocumented_columns": [n for n in spec.names if not spec.column_docs[n].documented],
        "cast_failures": _cast_failures(manifest),
        "evidence": evidence,
        "invariants": invariants,
        "utc_crosscheck": crosscheck,
        "columns": columns,
        "json_name": spec.dictionary_json,
    }


# ---------------------------------------------------------------- rendering
def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.2f}%"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _of(part: int, whole: int) -> str:
    return f"{_fmt_int(part)} of {_fmt_int(whole)} ({_pct(_rate(part, whole))})"


def _stats_cell(p: dict[str, Any]) -> str:
    if _all_null(p):
        return "all NULL"
    if "true_rate" in p:
        return f"TRUE {_pct(p['true_rate'])}"
    if "min" in p:
        if "median" in p:
            zero = f"; zero {_pct(p['zero_rate'])}" if p.get("zero_rate") is not None else ""
            return f"{p['min']} / {p['median']} / {p['max']}{zero}"
        return f"{p['min']} → {p['max']}"
    return ""


def _render_evidence_final_2017(ev: dict[str, Any]) -> list[str]:
    age, nc = ev["gh_repo_age"], ev["gh_repo_num_commits"]
    return [
        "## Evidence for undocumented columns",
        "",
        f"- `gh_repo_age`: {age['method']}: median slope **{age['median_slope_days_per_day']}** "
        f"days per day (p10 {age['p10_slope']}, p90 {age['p90_slope']}, {age['projects']} "
        "projects) — consistent with the repository's age in days at build time.",
        f"- `gh_repo_num_commits`: {nc['method']}: **{_pct(nc['non_decreasing_share'])}** of "
        f"{_fmt_int(nc['pairs'])} pairs; {nc['projects_with_more_than_one_value']} of "
        f"{nc['projects']} projects have more than one value — so it is not a single "
        "crawl-time snapshot per project. Exact definition unknown.",
        f"  - {nc['master_push_builds']['method']}: non-decreasing in "
        f"**{_pct(nc['master_push_builds']['non_decreasing_share'])}** and strictly increasing "
        f"in {_pct(nc['master_push_builds']['strictly_increasing_share'])} of "
        f"{_fmt_int(nc['master_push_builds']['pairs'])} pairs; the per-project slope over time "
        f"is positive in {nc['positive_time_slope_projects']} of {nc['slope_projects']} projects "
        "(>= 20 builds) — consistent with a commit count at build time.",
        "- `tr_log_num_test_suites_*`: meaning inferred from the name only (unverified).",
        "",
    ]


def _render_evidence_5_3_2016(ev: dict[str, Any]) -> list[str]:
    fc, dup = ev["gh_first_commit_created_at"], ev["duplicate_job_rows"]
    ta, fl = ev["tests_added_definition"], ev["file_count_definition"]
    gc, cm = ev["git_commits"], ev["git_num_committers"]
    london = _of(fc["equal_to_started_at_in_europe_london"], fc["rows"])
    file_le = _of(fl["categorised_le_touched"], fl["builds_measured"])
    return [
        "## Evidence (measured on the Java subset)",
        "",
        "Where the two page versions disagree, or the data contradicts them, these measurements "
        "decide. They describe correlations in the dump, not how the authors computed it.",
        "",
        f"- **`gh_first_commit_created_at` is not a commit timestamp.** It equals `tr_started_at` "
        f"rendered in Europe/London local time on {london} job rows; the offset to "
        f"`tr_started_at` is exactly 0 h on {_fmt_int(fc['offset_0h'])}, "
        f"+1 h on {_fmt_int(fc['offset_plus_1h'])} and +2 h on {_fmt_int(fc['offset_plus_2h'])} "
        f"rows. It varies per build ({fc['projects_with_a_single_value']} of {fc['projects']} "
        "projects have a single value), so it is not a repository start date either. Use "
        "`tr_started_at` (UTC); never treat this column as a commit time or as UTC.",
        f"- **Duplicate job rows are job rows attached to more than one build.** "
        f"{_fmt_int(dup['job_ids_on_more_than_one_row'])} of "
        f"{_fmt_int(dup['distinct_job_ids'])} job ids occur on more than one row "
        f"({_fmt_int(dup['rows_of_duplicated_ids'])} rows); "
        f"{_fmt_int(dup['job_ids_in_more_than_one_build'])} of them occur under more than one "
        f"`tr_build_id`. For {_fmt_int(dup['duplicated_ids_listed_on_exactly_one_row'])} of the "
        "duplicated ids exactly one row lists the job in its own build's `tr_jobs` "
        f"({_fmt_int(dup['duplicated_ids_listed_on_no_row'])}: none). Overall "
        f"{_of(dup['rows_listed_in_own_build'], dup['rows'])} job rows are listed in their own "
        f"build; {_fmt_int(dup['builds_without_a_listed_row'])} builds have no listed row. "
        "Staging should keep, per job id, the row listed in its own build.",
        f"- **`gh_tests_added` / `gh_tests_deleted` behave like test-case counts, not lines.** "
        f"Of {_fmt_int(ta['builds_with_test_churn'])} builds with test churn > 0, "
        f"{_fmt_int(ta['with_test_churn_but_no_tests_added_or_deleted'])} have neither tests "
        f"added nor deleted, and {_fmt_int(ta['tests_added_greater_than_test_churn'])} of "
        f"{_fmt_int(ta['builds_measured'])} builds have more tests added than test churn — both "
        "would be impossible for lines of test code (churn counts added + deleted lines).",
        f"- **File-type counts are per change, not per repository.** src + doc + other files <= "
        f"files added + deleted + modified on {file_le} builds (equal on "
        f"{_fmt_int(fl['categorised_eq_touched'])}).",
        f"- **`git_num_commits`** equals the number of `#`-separated entries of `git_commits` on "
        f"{_of(gc['num_commits_equals_entries'], gc['rows_measured'])} job rows; `git_commits` "
        f"contains `git_commit` on {_fmt_int(gc['git_commits_contains_git_commit'])} rows; "
        f"minimum {gc['min_git_num_commits']}.",
        f"- **`git_num_committers`** is <= `git_num_commits` + 1 on "
        f"{_of(cm['at_most_num_commits_plus_1'], cm['rows_measured'])} job rows and equals 1 on "
        f"{_fmt_int(cm['equal_to_1'])} — consistent with the committers of the built commits, "
        "not the project-wide count the page describes.",
        "",
    ]


_EVIDENCE_RENDERERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    SECONDARY: _render_evidence_final_2017,
    PRIMARY: _render_evidence_5_3_2016,
}


def _render_crosscheck(cc: dict[str, Any] | None) -> list[str]:
    lines = ["## UTC and status cross-check (secondary archive)", ""]
    if cc is None:
        return [
            *lines,
            "Not run for this build of the dictionary (pass `crosscheck_dir`, the monperrus "
            "`travistorrent-java-ci-build-dataset` archive, to `build_data_dictionary`).",
            "",
        ]
    n = cc["joined_java_builds"]
    return [
        *lines,
        f"Joined on the build id with `{cc['archive']}` ({cc['archive_files']} "
        f"`repo-data-travis.csv` files, {_fmt_int(cc['archive_rows'])} builds, "
        f"{_fmt_int(cc['archive_rows_with_explicit_utc'])} with an explicit `UTC` start time): "
        f"**{_fmt_int(n)} Java builds** from {cc['joined_projects']} projects.",
        "",
        "| Check | Agreeing builds |",
        "|---|---:|",
        f"| `{cc['started_at_column']}` = archive `started_at` (UTC) | "
        f"{_of(cc['started_at_equal'], n)} |",
        f"| … off by exactly 1 h or 2 h (a time-zone shift) | "
        f"{_of(cc['started_at_off_by_1h_or_2h'], n)} |",
        f"| `tr_status` = archive `status` | {_of(cc['status_equal'], n)} |",
        f"| `tr_duration` = archive `duration` | {_of(cc['duration_equal'], n)} |",
        "",
    ]


def render_markdown(d: dict[str, Any]) -> str:
    ds, c = d["dataset"], d["counts"]
    short = _SHORT.get(ds["key"], ds["key"])
    doc = d["dataformat_doc"]
    lines: list[str] = [
        f"# Data dictionary — {ds['title']}",
        "",
        "> **Generated** by `python -m rl_pipeline build_data_dictionary --dataset "
        f"{ds['key']}` from the converted Parquet of the real dataset. Do not edit by hand; "
        "every number below is computed. Machine-readable twin: "
        f"`docs/data/{d['json_name']}`.",
        "",
        "## Source",
        "",
        f"- Dataset key `{ds['key']}`. File: `{ds['filename']}` · "
        f"{_fmt_int(ds['size_bytes'])} bytes · md5 `{ds['md5']}` (= dataset version)",
        f"- Figshare DOI [{ds['doi']}](https://doi.org/{ds['doi']}) · download URL `{ds['url']}`",
        f"- License: **{ds['license']}**. Attribution: {ds['attribution']}",
        f"- Column meanings paraphrase the official data-format page ({doc['url']}, commit "
        f"`{doc['commit'][:12]}`)."
        + (f" {doc['note']}" if doc.get("note") else "")
        + (
            f" {len(d['undocumented_columns'])} columns are not on that page: "
            + ", ".join(f"`{n}`" for n in d["undocumented_columns"])
            + "."
            if d["undocumented_columns"]
            else ""
        ),
        f"- Timestamps: {ds['timestamp_semantics']}",
        "",
        "## Counts",
        "",
        "| Scope | Job rows | Distinct `tr_job_id` | Builds | Projects |",
        "|---|---:|---:|---:|---:|",
        f"| All languages | {_fmt_int(c['overall']['jobs'])} | "
        f"{_fmt_int(c['overall']['distinct_job_ids'])} | {_fmt_int(c['overall']['builds'])} | "
        f"{_fmt_int(c['overall']['projects'])} |",
    ]
    for lang, v in c["by_lang"].items():
        lines.append(
            f"| `gh_lang = {lang}` | {_fmt_int(v['jobs'])} | {_fmt_int(v['distinct_job_ids'])} | "
            f"{_fmt_int(v['builds'])} | {_fmt_int(v['projects'])} |"
        )
    rng = c["java_build_started_at_range"]
    java_builds = c["java"]["builds"]
    lines += [
        "",
        f"Java `{rng['column']}` range (build level, UTC): **{rng['min']} → {rng['max']}**.",
        "",
        "### Java label distribution (`tr_status`, one row per build)",
        "",
        "| tr_status | Builds | Share |",
        "|---|---:|---:|",
    ]
    for status, n in c["java_label_distribution"].items():
        share = n / java_builds if java_builds else 0.0
        lines.append(f"| `{status}` | {_fmt_int(n)} | {share * 100:.2f}% |")
    lines += [
        "",
        "Label rule (ADR-0005/0017): `failed`/`errored` → 1, `passed` → 0, `canceled` excluded. "
        "Any other status present above (e.g. `started`) is excluded as well (no observed "
        "outcome).",
        "",
        f"## Spec names → real {short} column names",
        "",
        (
            "The spec uses the 2016-style names; the 2017 dump renamed the change-set columns. "
            if short == "2017"
            else "The spec's names are this release's own names. "
        )
        + "Every target below was checked against the real header.",
        "",
        "| Spec name | Real column | Renamed | In header | Availability |",
        "|---|---|---|---|---|",
    ]
    for m in d["spec_name_map"]:
        lines.append(
            f"| `{m['spec_name']}` | `{m['column']}` | {'yes' if m['renamed'] else 'no'} | "
            f"{'yes' if m['present_in_header'] else '**NO**'} | {m['availability']} |"
        )
    lines += [
        "",
        "## rl_core build inputs → columns",
        "",
        "How each `rl_core.features.change.BUILD_INPUT_FIELDS` input is fed from this release "
        "(applied in staging/marts, M3). `direct`: same concept; `approximate`: a related "
        "definition (see note); `none`: not available, the input stays unknown (NaN). Rates are "
        "measured on Java builds.",
        "",
        "| Build input | Column | Status | Availability | Java null | Java zero | Note |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for m in d["build_input_map"]:
        col = f"`{m['column']}`" if m["column"] else "—"
        lines.append(
            f"| `{m['field']}` | {col} | {m['status']} | {m['availability'] or '—'} | "
            f"{_pct(m['java_null_rate'])} | {_pct(m['java_zero_rate'])} | {m['note']} |"
        )

    cols = d["columns"]
    by_avail: dict[str, list[str]] = {}
    for col in cols:
        by_avail.setdefault(col["availability"], []).append(col["name"])
    lines += [
        "",
        "## Availability summary",
        "",
        "- `pre_build` (known when the build is triggered): "
        f"{len(by_avail.get('pre_build', []))} columns, of which "
        f"{sum(1 for x in cols if x['role'] == 'feature_candidate')} are feature candidates "
        "and the rest identifiers/context/excluded (never model features).",
        f"- `post_build` (label, duration, build-log analysis): "
        f"{len(by_avail.get('post_build', []))} columns — forbidden for the current build; "
        "usable only as lagged history of builds that had finished.",
        "- `uncertain` (**excluded from model inputs by default**): "
        + ", ".join(f"`{n}`" for n in by_avail.get("uncertain", []))
        + ".",
        f"- `model_input_default` is true for {sum(1 for x in cols if x['model_input_default'])} "
        "feature candidates. Feature candidates measured as all-NULL or constant in the Java "
        "subset are excluded: "
        + (
            ", ".join(
                f"`{x['name']}`"
                for x in cols
                if x["role"] == "feature_candidate" and not x["model_input_default"]
            )
            or "none"
        )
        + ".",
        "",
        "## Measurement",
        "",
        f"- Subset: `{d['measurement']['subset']}`. Build-grain columns: "
        f"{d['measurement']['build_grain']}; job-grain columns: {d['measurement']['job_grain']}.",
        f"- Null = {d['measurement']['null_definition']}. Distinct = "
        f"{d['measurement']['distinct_count']}.",
        "- *Varying builds* = Java builds whose jobs disagree on the column (different values, "
        "or NULL mixed with a value) — a measured check of the declared grain.",
        "- Stats: min / median / max (and the share of zeros) for numeric feature/outcome "
        "columns, min → max for timestamps, TRUE share for booleans. Free-text values are never "
        "shown.",
        "",
        "## Data-quality flags (Java, measured)",
        "",
        "`all_null`: no value at all · `constant`: a single distinct value · `mostly_zero`: "
        f"≥ {MOSTLY_ZERO_THRESHOLD:.0%} of non-null values are 0. Flagged columns carry little "
        "or no signal in this dump; downstream feature selection must take this into account.",
        "",
    ]
    flagged = [x for x in cols if x["quality_flags"]]
    if flagged:
        lines += [
            "| Column | Flags | Null rate | Zero share | Distinct |",
            "|---|---|---:|---:|---:|",
        ]
        for col in flagged:
            p = col["java_profile"]
            zero = _pct(p["zero_rate"]) if p.get("zero_rate") is not None else "—"
            lines.append(
                f"| `{col['name']}` | {', '.join(col['quality_flags'])} | "
                f"{_pct(p['null_rate'])} | {zero} | {_fmt_int(p['distinct_count'])} |"
            )
    else:
        lines.append("No column is flagged.")
    cf = d["cast_failures"]
    lines += [
        "",
        "## Cast failures (conversion gate)",
        "",
        f"Non-missing raw values that failed their strict cast and became NULL ({cf['scope']}; "
        f"the gate allows at most {_pct(cf['max_failure_rate'])} per column).",
        "",
    ]
    if cf["columns"]:
        lines += ["| Column | Failed | Of non-missing |", "|---|---:|---:|"]
        for name, v in cf["columns"].items():
            lines.append(
                f"| `{name}` | {_fmt_int(v['cast_failures'])} | {_fmt_int(v['non_missing'])} |"
            )
    else:
        lines.append("None: every non-missing value parsed.")
    lines += [
        "",
        "## Columns",
        "",
        "| # | Column | DuckDB type | Grain | Availability | Role | Null rate | Distinct | "
        "Varying builds | Stats |",
        "|---:|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for col in cols:
        p = col["java_profile"]
        lines.append(
            f"| {col['position']} | `{col['name']}` | {col['duckdb_type']} | {col['grain']} | "
            f"{col['availability']} | {col['role']} | {_pct(p['null_rate'])} | "
            f"{_fmt_int(p['distinct_count'])} | {_fmt_int(col['java_builds_with_varying_values'])}"
            f" | {_stats_cell(p)} |"
        )
    lines += [
        "",
        "## Meanings and availability justifications",
        "",
        "| Column | Meaning | Availability — justification |",
        "|---|---|---|",
    ]
    for col in cols:
        lines.append(
            f"| `{col['name']}` | {col['meaning']} | **{col['availability']}** — "
            f"{col['justification']} |"
        )
    lines.append("")
    renderer = _EVIDENCE_RENDERERS.get(ds["key"])
    if renderer and d["evidence"]:
        lines += renderer(d["evidence"])
    lines += [
        "## Invariant checks (Java, measured)",
        "",
        "Facts that staging and leakage-safe features rely on. Violations are data errors or "
        "quirks of the dump; downstream code (dbt tests, feature functions) must tolerate or "
        "exclude them.",
        "",
        "| Rule | Violations | Of | Note |",
        "|---|---:|---|---|",
    ]
    for inv in d["invariants"].values():
        lines.append(
            f"| `{inv['rule']}` | {_fmt_int(inv['violations'])} | {_fmt_int(inv['of'])} "
            f"{inv['of_label']} | {inv['note']} |"
        )
    lines.append("")
    lines += _render_crosscheck(d.get("utc_crosscheck"))
    return "\n".join(lines)


def write_outputs(d: dict[str, Any], markdown_path: Path, json_path: Path) -> None:
    for path, text in (
        (json_path, json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False) + "\n"),
        (markdown_path, render_markdown(d)),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    log.info("data dictionary written", extra={"md": str(markdown_path), "json": str(json_path)})
