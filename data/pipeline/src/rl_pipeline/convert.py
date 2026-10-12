"""Stream the raw ``.csv.gz`` to typed, deterministic Parquet partitioned by language.

Layout (``<lake>/raw/travistorrent/version=<md5>/``)::

    lang=<gh_lang>/part-00000.parquet   one file per language, all 66 columns, typed
    _manifest.json                      written last; deterministic (no timings/timestamps)

Determinism: rows are ordered by ``(tr_build_id, tr_job_id)`` (a total order — ``tr_job_id`` is
required to be unique), with a fixed row-group size and ZSTD compression, so identical input
yields byte-identical files. The work is skipped when a manifest for the same version, schema and
converter settings exists and every file's sha256 still matches.

Data-quality gate (before anything is written): every column's cast-failure rate (values that are
neither NULL nor a null token but fail to parse, over non-missing values) must be <= 0.1%,
``gh_lang`` must be present and path-safe on every row, and ``tr_job_id`` must be unique.
Casts are strict (:data:`rl_pipeline.schema.STRICT_PATTERNS`): a malformed value is NULL and a
cast failure, never silently rounded or reinterpreted.
The CSV is never decompressed to disk; DuckDB reads the gzip stream directly.

Disk: the per-language ``ORDER BY`` spills to ``<out_root>/.duckdb_tmp-<pid>``. Measured in review
on the full file (memory_limit 1GB, 4 threads, 2026-10-13): **peak spill 1,267,624 KiB
(~1.2 GiB)**, peak ``out_root`` usage 1,380,616 KiB including the staged Parquet. The spill is
capped explicitly (``SET max_temp_directory_size``) at ``min(DuckDBLimits.max_temp_mib, free disk -
output estimate - reserve)``, so a conversion fails cleanly with :class:`InsufficientDiskError`
instead of eating the host's last free space. Work directories of crashed runs (dead pid) are
swept on the next call; a failed run removes its own.
"""

# ruff: noqa: S608 - SQL is assembled from schema constants; every value goes through sql_literal()

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from rl_pipeline import __version__
from rl_pipeline.hashing import sha256_file
from rl_pipeline.schema import (
    EXPECTED_COLUMNS,
    NULL_TOKENS,
    PARTITION_COLUMN,
    SORT_KEY,
    STRICT_PATTERNS,
    TIMESTAMP_FORMAT,
    read_header,
    schema_fingerprint,
    sql_literal,
    validate_header,
)

log = logging.getLogger("rl_pipeline.convert")

CONVERTER_VERSION = "1"
ROW_GROUP_SIZE = 122_880
COMPRESSION = "zstd"
CAST_FAILURE_MAX_RATE = 0.001
PART_FILENAME = "part-00000.parquet"
MANIFEST_NAME = "_manifest.json"
_LANG_RE = re.compile(r"^[a-z0-9][a-z0-9_+-]{0,31}$")
# Free space left untouched for the rest of the host (same margin as dataset downloads).
DISK_RESERVE_BYTES = 256 * 1024 * 1024
_MIB = 1024 * 1024
# Work dirs this module (and the dictionary builder) create next to the version dirs.
_WORK_DIR_RE = re.compile(
    r"^\.(?:staging-version=[0-9a-f]{32}|old-version=[0-9a-f]{32}|duckdb_tmp(?:-dict)?)-(\d+)$"
)

_POLARS_TYPES: dict[str, pl.DataType] = {
    "BIGINT": pl.Int64(),
    "INTEGER": pl.Int32(),
    "DOUBLE": pl.Float64(),
    "BOOLEAN": pl.Boolean(),
    "VARCHAR": pl.String(),
    "TIMESTAMP": pl.Datetime("us"),
}


class DataQualityError(ValueError):
    """The raw data failed the conversion gate; nothing was written."""


class InsufficientDiskError(RuntimeError):
    """Not enough free disk for the conversion's output plus DuckDB spill; nothing was kept."""


@dataclass(frozen=True, slots=True)
class DuckDBLimits:
    memory_limit: str = "1GB"
    threads: int = 4
    # Upper bound for DuckDB's spill directory (further capped by free disk, see temp_budget).
    max_temp_mib: int = 3072


def connect(
    limits: DuckDBLimits, temp_dir: Path | None = None, *, max_temp_bytes: int | None = None
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit = {sql_literal(limits.memory_limit)}")
    con.execute(f"SET threads = {int(limits.threads)}")
    con.execute("SET preserve_insertion_order = true")
    con.execute("SET enable_progress_bar = false")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = {sql_literal(str(temp_dir))}")
    if max_temp_bytes is not None:
        con.execute(f"SET max_temp_directory_size = {sql_literal(f'{int(max_temp_bytes)}B')}")
    return con


def temp_budget(out_root: Path, output_estimate: int, limits: DuckDBLimits) -> int:
    """Bytes DuckDB may spill: ``min(max_temp_mib, free - output_estimate - reserve)``.

    Raises :class:`InsufficientDiskError` up front when even the output would not fit.
    """
    probe = out_root
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    budget = free - output_estimate - DISK_RESERVE_BYTES
    if budget <= 0:
        raise InsufficientDiskError(
            f"{free} bytes free under {probe}; need > {output_estimate} (output estimate) + "
            f"{DISK_RESERVE_BYTES} (reserve) before any DuckDB spill"
        )
    return min(int(limits.max_temp_mib) * _MIB, budget)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def sweep_stale_work_dirs(root: Path) -> list[str]:
    """Remove staging/old/spill dirs left by runs whose pid is no longer alive (e.g. OOM-killed).

    Dirs of live processes (including a concurrent conversion) are never touched. Pids are only
    meaningful inside one pid namespace, so never share one lake directory between the host and
    a container at the same time (compose uses a separate ``lake`` volume).
    """
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for path in sorted(root.iterdir()):
        match = _WORK_DIR_RE.match(path.name)
        if not match or not path.is_dir():
            continue
        pid = int(match.group(1))
        if pid == os.getpid() or _pid_alive(pid):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    if removed:
        log.warning("removed stale work dirs of dead runs", extra={"dirs": removed})
    return removed


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _raw_source(csv_path: Path) -> str:
    """read_csv with no type sniffing: every column VARCHAR, names from the expected schema."""
    cols = ", ".join(f"{sql_literal(n)}: 'VARCHAR'" for n, _ in EXPECTED_COLUMNS)
    return (
        f"read_csv({sql_literal(str(csv_path))}, header=true, auto_detect=false, "
        f"delim=',', quote='\"', escape='\"', compression='gzip', columns={{{cols}}})"
    )


def _non_missing(name: str) -> str:
    tokens = ", ".join(sql_literal(t) for t in NULL_TOKENS)
    raw = _ident(name)
    return f"CASE WHEN {raw} IN ({tokens}) THEN NULL ELSE {raw} END"


def typed_expr(name: str, duck_type: str) -> str:
    """SQL turning the raw VARCHAR column into its target type (NULL on failure).

    Strict: a value must fully match :data:`STRICT_PATTERNS` for its type before it is cast.
    """
    nn = _non_missing(name)
    if duck_type == "VARCHAR":
        return nn
    if duck_type == "BOOLEAN":
        return f"CASE upper({nn}) WHEN 'TRUE' THEN TRUE WHEN 'FALSE' THEN FALSE END"
    guard = f"regexp_full_match({nn}, {sql_literal(STRICT_PATTERNS[duck_type])})"
    if duck_type == "TIMESTAMP":
        return f"CASE WHEN {guard} THEN TRY_STRPTIME({nn}, {sql_literal(TIMESTAMP_FORMAT)}) END"
    return f"CASE WHEN {guard} THEN TRY_CAST({nn} AS {duck_type}) END"


def _typed_select() -> str:
    return ", ".join(f"{typed_expr(n, t)} AS {_ident(n)}" for n, t in EXPECTED_COLUMNS)


# ---------------------------------------------------------------- profiling + gate
def profile_raw(con: duckdb.DuckDBPyConnection, csv_path: Path) -> dict[str, Any]:
    """One streaming pass: rows per language, per-column non-missing and cast-failure counts."""
    aggs: list[str] = ["count(*) AS __rows"]
    for name, duck_type in EXPECTED_COLUMNS:
        nn = _non_missing(name)
        aggs.append(f"count({nn}) AS {_ident('nn__' + name)}")
        if duck_type != "VARCHAR":
            aggs.append(
                f"count(*) FILTER (WHERE ({nn}) IS NOT NULL AND ({typed_expr(name, duck_type)}) "
                f"IS NULL) AS {_ident('fail__' + name)}"
            )
    lang = _non_missing(PARTITION_COLUMN)
    sql = (
        f"SELECT {lang} AS __lang, {', '.join(aggs)} FROM {_raw_source(csv_path)} "
        "GROUP BY ALL ORDER BY __lang NULLS FIRST"
    )
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    job_id = typed_expr("tr_job_id", "BIGINT")
    build_id = typed_expr("tr_build_id", "BIGINT")
    key_row = con.execute(
        f"SELECT count({job_id}) - count(DISTINCT {job_id}), "
        f"count(*) FILTER (WHERE {job_id} IS NULL), "
        f"count(*) FILTER (WHERE {build_id} IS NULL) FROM {_raw_source(csv_path)}"
    ).fetchone()
    assert key_row is not None

    columns: dict[str, dict[str, Any]] = {}
    for name, duck_type in EXPECTED_COLUMNS:
        non_missing = int(sum(r["nn__" + name] for r in rows))
        failures = int(sum(r.get("fail__" + name, 0) for r in rows))
        rate = failures / non_missing if non_missing else 0.0
        columns[name] = {
            "type": duck_type,
            "non_missing": non_missing,
            "cast_failures": failures,
            "cast_failure_rate": round(rate, 8),
        }
    return {
        "rows_total": int(sum(r["__rows"] for r in rows)),
        "rows_per_lang": {
            ("<missing>" if r["__lang"] is None else str(r["__lang"])): int(r["__rows"])
            for r in rows
        },
        "columns": columns,
        "duplicate_job_ids": int(key_row[0]),
        "null_job_ids": int(key_row[1]),
        "null_build_ids": int(key_row[2]),
    }


def quality_gate(profile: dict[str, Any], max_rate: float = CAST_FAILURE_MAX_RATE) -> None:
    problems: list[str] = []
    for name, col in profile["columns"].items():
        if col["cast_failure_rate"] > max_rate:
            problems.append(
                f"{name} ({col['type']}): {col['cast_failures']} of {col['non_missing']} "
                f"non-missing values failed to cast ({col['cast_failure_rate']:.4%} > "
                f"{max_rate:.1%})"
            )
    for lang in profile["rows_per_lang"]:
        if not _LANG_RE.match(lang):
            problems.append(f"{PARTITION_COLUMN} value {lang!r} is missing or not path-safe")
    if profile["duplicate_job_ids"] or profile["null_job_ids"] or profile["null_build_ids"]:
        problems.append(
            f"key violation: {profile['duplicate_job_ids']} duplicate tr_job_id, "
            f"{profile['null_job_ids']} null tr_job_id, {profile['null_build_ids']} null "
            "tr_build_id"
        )
    if problems:
        raise DataQualityError("data-quality gate failed: " + "; ".join(problems))


# ---------------------------------------------------------------- manifest / skip path
def _settings_block() -> dict[str, Any]:
    return {
        "converter_version": CONVERTER_VERSION,
        "duckdb_version": duckdb.__version__,
        "compression": COMPRESSION,
        "row_group_size": ROW_GROUP_SIZE,
        "order_by": list(SORT_KEY),
        "partition_by": PARTITION_COLUMN,
        "schema_fingerprint": schema_fingerprint(),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_manifest(local_dir: Path) -> dict[str, Any] | None:
    path = local_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _manifest_still_valid(local_dir: Path, version: str) -> tuple[dict[str, Any] | None, str]:
    try:
        manifest = read_manifest(local_dir)
    except (OSError, ValueError) as exc:  # unreadable / not JSON: regenerate, don't crash
        return None, f"unreadable manifest ({exc.__class__.__name__})"
    if manifest is None:
        return None, "no manifest"
    if manifest.get("version") != version:
        return None, "manifest version differs"
    if manifest.get("converter") != _settings_block():
        return None, "converter settings or schema changed"
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return None, "manifest lists no files"
    for f in files:
        if not isinstance(f, dict) or not {"path", "sha256", "rows", "lang", "size"} <= set(f):
            return None, "malformed file entry in manifest"
        path = local_dir / str(f["path"])
        if not path.is_file():
            return None, f"missing file {f['path']}"
        if sha256_file(path) != f["sha256"]:
            return None, f"sha256 mismatch for {f['path']}"
    return manifest, "manifest and all file checksums match"


def _result(
    manifest: dict[str, Any], local_dir: Path, *, skipped: bool, seconds: float
) -> dict[str, Any]:
    manifest_path = local_dir / MANIFEST_NAME
    return {
        "version": manifest["version"],
        "local_dir": str(local_dir),
        "files": [
            {
                "path": f["path"],
                "rows": f["rows"],
                "sha256": f["sha256"],
                "lang": f["lang"],
                "size": f["size"],
            }
            for f in manifest["files"]
        ],
        "rows_total": manifest["rows_total"],
        "rows_per_lang": manifest["rows_per_lang"],
        "parquet_bytes_total": sum(f["size"] for f in manifest["files"]),
        "cast_failures_total": sum(
            c["cast_failures"] for c in manifest["cast_profile"]["columns"].values()
        ),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "skipped": skipped,
        "conversion_seconds": round(seconds, 3),
    }


def _verify_parquet_schema(path: Path) -> None:
    schema = pl.read_parquet_schema(path)
    actual = [(n, schema[n]) for n in schema]
    expected = [(n, _POLARS_TYPES[t]) for n, t in EXPECTED_COLUMNS]
    if actual != expected:
        raise DataQualityError(f"{path} has an unexpected Parquet schema: {actual}")


def _write_partitions(
    con: duckdb.DuckDBPyConnection, csv_path: Path, staging: Path, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """One sorted, typed Parquet file per language under ``staging``; verified after writing."""
    files: list[dict[str, Any]] = []
    order = ", ".join(_ident(c) for c in SORT_KEY)
    for lang in sorted(profile["rows_per_lang"]):
        rel = f"lang={lang}/{PART_FILENAME}"
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY (SELECT {_typed_select()} FROM {_raw_source(csv_path)} "
            f"WHERE {_ident(PARTITION_COLUMN)} = {sql_literal(lang)} ORDER BY {order}) "
            f"TO {sql_literal(str(target))} (FORMAT parquet, COMPRESSION {COMPRESSION}, "
            f"ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
        )
        row = con.execute(
            f"SELECT count(*) FROM read_parquet({sql_literal(str(target))})"
        ).fetchone()
        rows = int(row[0]) if row else -1
        if rows != profile["rows_per_lang"][lang]:
            raise DataQualityError(
                f"{rel}: wrote {rows} rows, expected {profile['rows_per_lang'][lang]}"
            )
        _verify_parquet_schema(target)
        files.append(
            {
                "path": rel,
                "lang": lang,
                "rows": rows,
                "size": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        )
        log.info("partition written", extra=files[-1])
    return files


# ---------------------------------------------------------------- conversion
def convert(
    csv_path: Path,
    version: str,
    out_root: Path,
    *,
    source: dict[str, Any] | None = None,
    limits: DuckDBLimits | None = None,
    max_cast_failure_rate: float = CAST_FAILURE_MAX_RATE,
) -> dict[str, Any]:
    """Convert ``csv_path`` into ``<out_root>/version=<version>/`` (see module docstring)."""
    if not re.fullmatch(r"[0-9a-f]{32}", version):
        raise ValueError(f"version must be a 32-char lowercase hex md5, got {version!r}")
    started = time.perf_counter()
    local_dir = out_root / f"version={version}"

    manifest, reason = _manifest_still_valid(local_dir, version)
    if manifest is not None:
        log.info("conversion skipped", extra={"local_dir": str(local_dir), "reason": reason})
        return _result(manifest, local_dir, skipped=True, seconds=time.perf_counter() - started)
    log.info("converting", extra={"local_dir": str(local_dir), "reason": reason})

    limits = limits or DuckDBLimits()
    out_root.mkdir(parents=True, exist_ok=True)
    sweep_stale_work_dirs(out_root)
    # The input size is a safe upper bound for the ZSTD Parquet output (measured: 207,787,946 B
    # of Parquet for the 264,607,174 B .csv.gz).
    max_temp = temp_budget(out_root, csv_path.stat().st_size, limits)
    tmp_dir = out_root / f".duckdb_tmp-{os.getpid()}"
    staging = out_root / f".staging-version={version}-{os.getpid()}"
    old = out_root / f".old-version={version}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    log.info("duckdb spill cap", extra={"max_temp_bytes": max_temp, "temp_dir": str(tmp_dir)})
    try:
        con = connect(limits, tmp_dir, max_temp_bytes=max_temp)
        try:
            validate_header(read_header(con, csv_path))

            t0 = time.perf_counter()
            profile = profile_raw(con, csv_path)
            profile_seconds = time.perf_counter() - t0
            quality_gate(profile, max_cast_failure_rate)

            t1 = time.perf_counter()
            files = _write_partitions(con, csv_path, staging, profile)
            write_seconds = time.perf_counter() - t1
        except duckdb.OutOfMemoryException as exc:
            raise InsufficientDiskError(
                f"DuckDB ran out of memory/spill space (memory_limit={limits.memory_limit}, "
                f"spill cap={max_temp} bytes); free disk under {out_root} or raise "
                f"RL_DUCKDB_MAX_TEMP_MIB: {exc}"
            ) from exc
        finally:
            con.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)

        manifest = {
            "dataset": "travistorrent",
            "version": version,
            "source": source or {},
            "converter": _settings_block(),
            "schema": [{"name": n, "type": t} for n, t in EXPECTED_COLUMNS],
            "null_tokens": list(NULL_TOKENS),
            "timestamp_format": TIMESTAMP_FORMAT,
            "timestamp_semantics": "UTC wall-clock stored as TIMESTAMP (no zone)",
            "rows_total": profile["rows_total"],
            "rows_per_lang": profile["rows_per_lang"],
            "cast_profile": {
                "max_failure_rate": max_cast_failure_rate,
                "columns": profile["columns"],
                "duplicate_job_ids": profile["duplicate_job_ids"],
            },
            "files": files,
            "producer": f"rl_pipeline {__version__}",
        }
        _write_json(staging / MANIFEST_NAME, manifest)

        # Swap the finished staging dir into place; the manifest's presence implies completeness.
        if local_dir.exists():
            local_dir.replace(old)
        try:
            staging.replace(local_dir)
        except OSError:
            if old.exists() and not local_dir.exists():
                old.replace(local_dir)  # put the previous version back
            raise
    except BaseException:
        # Never leave a half-written staging dir (up to the full output size) behind.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)

    result = _result(manifest, local_dir, skipped=False, seconds=time.perf_counter() - started)
    result["profile_seconds"] = round(profile_seconds, 3)
    result["write_seconds"] = round(write_seconds, 3)
    log.info(
        "conversion finished",
        extra={k: v for k, v in result.items() if k != "files"},
    )
    return result
