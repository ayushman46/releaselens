"""Generate ``docs/data/DATA_DICTIONARY.md`` + ``data_dictionary.json`` from the converted Parquet.

Every number is computed from the data at build time; meanings and availability tags come from
:mod:`rl_pipeline.catalog`. Measurements use the **Java subset** (``gh_lang = 'java'``):

* build-grain columns: one row per ``tr_build_id`` (the row with the lowest ``tr_job_id``);
* job-grain columns (``tr_job_id``, ``tr_log_*``): every job row.

No raw values of free-text columns are written. Numeric/timestamp columns get min/median/max,
booleans their TRUE share; the only categorical values shown are ``gh_lang`` and the
``tr_status`` label distribution. Output is deterministic (sorted keys, no generation clock).
"""

# ruff: noqa: S608 - SQL is assembled from schema constants; every value goes through sql_literal()

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from rl_pipeline.catalog import (
    COLUMN_DOCS,
    DATAFORMAT_COMMIT,
    DATAFORMAT_SHA256,
    DATAFORMAT_URL,
    SPEC_NAME_MAP,
)
from rl_pipeline.convert import PART_FILENAME, DuckDBLimits, connect, read_manifest, temp_budget
from rl_pipeline.dataset import TRAVISTORRENT_FIGSHARE
from rl_pipeline.schema import EXPECTED_COLUMNS, EXPECTED_NAMES, sql_literal

log = logging.getLogger("rl_pipeline.dictionary")

JAVA = "java"
_NUMERIC = {"BIGINT", "INTEGER", "DOUBLE"}
MOSTLY_ZERO_THRESHOLD = 0.95


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _num(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float):
        return round(value, 4)
    return value


def _setup_views(con: duckdb.DuckDBPyConnection, local_dir: Path) -> None:
    glob = str(local_dir / "lang=*" / PART_FILENAME)
    java_file = local_dir / f"lang={JAVA}" / PART_FILENAME
    if not java_file.is_file():
        raise FileNotFoundError(f"{java_file} missing: the dictionary is measured on Java")
    con.execute(
        "CREATE OR REPLACE VIEW jobs AS SELECT * FROM "
        f"read_parquet({sql_literal(glob)}, hive_partitioning=false)"
    )
    con.execute(
        "CREATE OR REPLACE VIEW java_jobs AS SELECT * FROM "
        f"read_parquet({sql_literal(str(java_file))}, hive_partitioning=false) "
        f"WHERE gh_lang = {sql_literal(JAVA)}"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE java_builds AS SELECT * FROM java_jobs "
        "QUALIFY row_number() OVER (PARTITION BY tr_build_id ORDER BY tr_job_id) = 1"
    )


def _counts(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    def one(sql: str) -> tuple[Any, ...]:
        row = con.execute(sql).fetchone()
        assert row is not None
        return row

    agg = "count(*), count(DISTINCT tr_build_id), count(DISTINCT gh_project_name)"
    total = one(f"SELECT {agg} FROM jobs")
    by_lang = con.execute(f"SELECT gh_lang, {agg} FROM jobs GROUP BY 1 ORDER BY 1").fetchall()
    java = one(f"SELECT {agg} FROM java_jobs")
    rng = one("SELECT min(gh_build_started_at), max(gh_build_started_at) FROM java_builds")
    labels = con.execute(
        "SELECT tr_status, count(*) FROM java_builds GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
    keys = ("jobs", "builds", "projects")
    return {
        "overall": dict(zip(keys, map(int, total), strict=True)),
        "by_lang": {str(r[0]): dict(zip(keys, map(int, r[1:]), strict=True)) for r in by_lang},
        "java": dict(zip(keys, map(int, java), strict=True)),
        "java_build_started_at_range": {"min": _num(rng[0]), "max": _num(rng[1])},
        "java_label_distribution": {
            ("<null>" if r[0] is None else str(r[0])): int(r[1]) for r in labels
        },
    }


def _varying_builds(con: duckdb.DuckDBPyConnection, chunk: int = 8) -> dict[str, int]:
    """Per column: Java builds whose jobs disagree (different values, or NULL mixed with value).

    ``min <> max`` instead of ``count(DISTINCT) > 1`` keeps per-group state constant-size.
    """
    out: dict[str, int] = {}
    names = list(EXPECTED_NAMES)
    for i in range(0, len(names), chunk):
        part = names[i : i + chunk]
        flags = ", ".join(
            f"(coalesce(min({_q(n)}) <> max({_q(n)}), false) OR "
            f"(count({_q(n)}) > 0 AND count({_q(n)}) < count(*)))::INTEGER AS {_q(n)}"
            for n in part
        )
        sums = ", ".join(f"sum({_q(n)})" for n in part)
        row = con.execute(
            f"SELECT {sums} FROM (SELECT {flags} FROM java_jobs GROUP BY tr_build_id)"
        ).fetchone()
        assert row is not None
        out.update({n: int(v or 0) for n, v in zip(part, row, strict=True)})
    return out


def _column_profile(con: duckdb.DuckDBPyConnection, name: str, duck_type: str) -> dict[str, Any]:
    doc = COLUMN_DOCS[name]
    table = "java_jobs" if doc.grain == "job" else "java_builds"
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
    row = con.execute(f"SELECT {', '.join(exprs)} FROM {table}").fetchone()
    assert row is not None
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


def _evidence(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Measured behaviour of the two undocumented repository columns (Java builds)."""
    age = con.execute(
        "SELECT median(sl), quantile_cont(sl, 0.1), quantile_cont(sl, 0.9), count(*) FROM ("
        "SELECT regr_slope(gh_repo_age, epoch(gh_build_started_at) / 86400.0) AS sl "
        "FROM java_builds GROUP BY gh_project_name HAVING count(*) >= 20) WHERE sl IS NOT NULL"
    ).fetchone()
    commits = con.execute(
        "SELECT avg((cur >= prev)::INTEGER), count(*) FROM ("
        "SELECT gh_repo_num_commits AS cur, lag(gh_repo_num_commits) OVER ("
        "PARTITION BY gh_project_name ORDER BY gh_build_started_at, tr_build_id) AS prev "
        "FROM java_builds) WHERE prev IS NOT NULL AND cur IS NOT NULL"
    ).fetchone()
    varying = con.execute(
        "SELECT count(*) FILTER (WHERE d > 1), count(*) FROM (SELECT "
        "count(DISTINCT gh_repo_num_commits) AS d FROM java_builds GROUP BY gh_project_name)"
    ).fetchone()
    master = con.execute(
        "SELECT avg((cur >= prev)::INTEGER), avg((cur > prev)::INTEGER), count(*) FROM ("
        "SELECT gh_repo_num_commits AS cur, lag(gh_repo_num_commits) OVER ("
        "PARTITION BY gh_project_name ORDER BY gh_build_started_at, tr_build_id) AS prev "
        "FROM java_builds WHERE git_branch = 'master' AND NOT gh_is_pr) "
        "WHERE prev IS NOT NULL AND cur IS NOT NULL"
    ).fetchone()
    commit_slope = con.execute(
        "SELECT count(*) FILTER (WHERE sl > 0), count(*) FROM ("
        "SELECT regr_slope(gh_repo_num_commits, epoch(gh_build_started_at) / 86400.0) AS sl "
        "FROM java_builds GROUP BY gh_project_name HAVING count(*) >= 20) WHERE sl IS NOT NULL"
    ).fetchone()
    assert age is not None
    assert commits is not None
    assert varying is not None
    assert master is not None
    assert commit_slope is not None
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


def _invariants(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Measured violations of ordering facts that leakage-safe features rely on (Java builds)."""
    pr = con.execute(
        "SELECT count(*) FILTER (WHERE gh_pr_created_at > gh_build_started_at), "
        "count(*) FILTER (WHERE gh_is_pr), "
        "max(epoch(gh_pr_created_at) - epoch(gh_build_started_at)) FILTER ("
        "WHERE gh_pr_created_at > gh_build_started_at) / 3600.0 FROM java_builds"
    ).fetchone()
    prev = con.execute(
        "SELECT count(*) FILTER (WHERE tr_prev_build >= tr_build_id), count(tr_prev_build) "
        "FROM java_builds"
    ).fetchone()
    assert pr is not None
    assert prev is not None
    return {
        "pr_created_after_build_start": {
            "rule": "gh_pr_created_at <= gh_build_started_at (a PR build needs an open PR)",
            "violations": int(pr[0]),
            "pr_builds": int(pr[1]),
            "max_hours_after": _num(None if pr[2] is None else float(pr[2])),
        },
        "prev_build_not_earlier": {
            "rule": "tr_prev_build < tr_build_id (the previous build was created earlier)",
            "violations": int(prev[0]),
            "builds_with_prev": int(prev[1]),
        },
    }


def build_dictionary(local_dir: Path, limits: DuckDBLimits | None = None) -> dict[str, Any]:
    manifest = read_manifest(local_dir)
    if manifest is None:
        raise FileNotFoundError(f"{local_dir}/_manifest.json missing: convert first")
    limits = limits or DuckDBLimits()
    tmp_dir = local_dir.parent / f".duckdb_tmp-dict-{os.getpid()}"
    con = connect(limits, tmp_dir, max_temp_bytes=temp_budget(local_dir.parent, 0, limits))
    try:
        _setup_views(con, local_dir)
        counts = _counts(con)
        varying = _varying_builds(con)
        columns: list[dict[str, Any]] = []
        for pos, (name, duck_type) in enumerate(EXPECTED_COLUMNS, start=1):
            doc = COLUMN_DOCS[name]
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
            entry["java_profile"] = _column_profile(con, name, duck_type)
            entry["quality_flags"] = quality_flags(entry["java_profile"], doc.role)
            # A column with no value or a single value can never be a useful model input.
            if {"all_null", "constant"} & set(entry["quality_flags"]):
                entry["model_input_default"] = False
            columns.append(entry)
        evidence = _evidence(con)
        invariants = _invariants(con)
    finally:
        con.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    header = set(EXPECTED_NAMES)
    source = manifest.get("source") or {}
    return {
        "title": "TravisTorrent (Figshare final-2017-01-25) data dictionary",
        "generator": "rl_pipeline.dictionary (python -m rl_pipeline build_data_dictionary)",
        "dataset": {
            "version": manifest["version"],
            "md5": source.get("md5", manifest["version"]),
            "filename": source.get("filename", TRAVISTORRENT_FIGSHARE.filename),
            "size_bytes": source.get("size", TRAVISTORRENT_FIGSHARE.size),
            "doi": TRAVISTORRENT_FIGSHARE.doi,
            "url": TRAVISTORRENT_FIGSHARE.url,
            "license": TRAVISTORRENT_FIGSHARE.license,
            "attribution": TRAVISTORRENT_FIGSHARE.citation,
        },
        "dataformat_doc": {
            "url": DATAFORMAT_URL,
            "commit": DATAFORMAT_COMMIT,
            "sha256": DATAFORMAT_SHA256,
        },
        "measurement": {
            "subset": "gh_lang = 'java'",
            "build_grain": "one row per tr_build_id (the job row with the lowest tr_job_id)",
            "job_grain": "every job row",
            "distinct_count": "exact count(DISTINCT), NULL excluded",
            "null_definition": "SQL NULL after conversion (raw empty field or the 'NA' token)",
            "source_files": [f["path"] for f in manifest["files"]],
        },
        "counts": counts,
        "spec_name_map": [
            {
                "spec_name": spec,
                "column": real,
                "renamed": spec != real,
                "present_in_header": real in header,
                "availability": COLUMN_DOCS[real].availability if real in header else None,
            }
            for spec, real in SPEC_NAME_MAP.items()
        ],
        "undocumented_columns": [n for n in EXPECTED_NAMES if not COLUMN_DOCS[n].documented],
        "evidence": evidence,
        "invariants": invariants,
        "columns": columns,
    }


# ---------------------------------------------------------------- rendering
def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.2f}%"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


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


def render_markdown(d: dict[str, Any]) -> str:
    ds, c = d["dataset"], d["counts"]
    lines: list[str] = [
        "# Data dictionary — TravisTorrent (Figshare `final-2017-01-25`)",
        "",
        "> **Generated** by `python -m rl_pipeline build_data_dictionary` from the converted "
        "Parquet of the real dataset. Do not edit by hand; every number below is computed. "
        "Machine-readable twin: `docs/data/data_dictionary.json`.",
        "",
        "## Source",
        "",
        f"- File: `{ds['filename']}` · {_fmt_int(ds['size_bytes'])} bytes · md5 `{ds['md5']}` "
        f"(= dataset version)",
        f"- Figshare DOI [{ds['doi']}](https://doi.org/{ds['doi']}) · download URL `{ds['url']}`",
        f"- License: **{ds['license']}**. Attribution: {ds['attribution']}",
        f"- Column meanings paraphrase the official data-format page ({d['dataformat_doc']['url']}"
        f", commit `{d['dataformat_doc']['commit'][:12]}`). "
        f"{len(d['undocumented_columns'])} columns are not on that page: "
        + ", ".join(f"`{n}`" for n in d["undocumented_columns"])
        + ".",
        "",
        "## Counts",
        "",
        "| Scope | Jobs (rows) | Builds | Projects |",
        "|---|---:|---:|---:|",
        f"| All languages | {_fmt_int(c['overall']['jobs'])} | {_fmt_int(c['overall']['builds'])}"
        f" | {_fmt_int(c['overall']['projects'])} |",
    ]
    for lang, v in c["by_lang"].items():
        lines.append(
            f"| `gh_lang = {lang}` | {_fmt_int(v['jobs'])} | {_fmt_int(v['builds'])} | "
            f"{_fmt_int(v['projects'])} |"
        )
    rng = c["java_build_started_at_range"]
    java_builds = c["java"]["builds"]
    lines += [
        "",
        f"Java `gh_build_started_at` range (build level, UTC): **{rng['min']} → {rng['max']}**.",
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
        "Label rule (ADR-0005): `failed`/`errored` → 1, `passed` → 0, `canceled` excluded. Any "
        "other status present above is excluded as well (no observed outcome).",
        "",
        "## Spec names → real 2017 column names",
        "",
        "The spec uses the 2016-style names; the 2017 dump renamed the change-set columns. Every "
        "target below was checked against the real header.",
        "",
        "| Spec name | Real column | Renamed | In header | Availability |",
        "|---|---|---|---|---|",
    ]
    for m in d["spec_name_map"]:
        lines.append(
            f"| `{m['spec_name']}` | `{m['column']}` | {'yes' if m['renamed'] else 'no'} | "
            f"{'yes' if m['present_in_header'] else '**NO**'} | {m['availability']} |"
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
        "and the rest identifiers/context (never model features).",
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
    ev = d["evidence"]
    age, nc = ev["gh_repo_age"], ev["gh_repo_num_commits"]
    lines += [
        "",
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
        "## Invariant checks (Java builds, measured)",
        "",
        "Ordering facts that leakage-safe features rely on. Violations are data errors in the "
        "dump; downstream code (dbt tests, feature functions) must tolerate or exclude them.",
        "",
        "| Rule | Violations | Of |",
        "|---|---:|---:|",
    ]
    inv = d["invariants"]
    pr, prev = inv["pr_created_after_build_start"], inv["prev_build_not_earlier"]
    after = (
        f" (up to {pr['max_hours_after']} h after the build started)" if pr["violations"] else ""
    )
    lines += [
        f"| `{pr['rule']}` | {_fmt_int(pr['violations'])}{after} | "
        f"{_fmt_int(pr['pr_builds'])} PR builds |",
        f"| `{prev['rule']}` | {_fmt_int(prev['violations'])} | "
        f"{_fmt_int(prev['builds_with_prev'])} builds with a previous build |",
        "",
    ]
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
