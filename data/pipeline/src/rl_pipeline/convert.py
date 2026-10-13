"""Stream a raw TravisTorrent ``.csv.gz`` to typed, deterministic Parquet partitioned by language.

Everything dataset-specific (columns, cast patterns, sort and unique keys, manifest extras) comes
from a :class:`rl_pipeline.datasets.DatasetSpec`.
Layout (``<lake>/raw/travistorrent/version=<md5>/``)::

    lang=<gh_lang>/part-00000.parquet   one file per language, every column, typed
    _manifest.json                      written last; deterministic (no timings/timestamps)

Determinism: rows are ordered by the spec's ``sort_key`` (``(tr_build_id, tr_job_id)`` for the
2017 release, ``(tr_build_id, tr_job_id, row)`` for 2016), which ends in the spec's
``unique_column`` and is therefore a total order; with a fixed row-group size and ZSTD
compression, identical input yields byte-identical files. The work is skipped when a manifest for
the same version, schema and converter settings exists and every file's sha256 still matches.

Raw stays faithful: no deduplication and no placeholder remapping. Only the null tokens
(``NA``) and empty fields become NULL; ``-1`` placeholders stay ``-1`` (staging decides).

Data-quality gate (before anything is written): every column's cast-failure rate (values that are
neither NULL nor a null token but fail to parse, over non-missing values) must be <= 0.1%, the
partition column must be present and path-safe on every row, the unique column must be non-null
and unique, and ``tr_job_id`` / ``tr_build_id`` must be non-null.
Casts are strict (:data:`rl_pipeline.schema.STRICT_PATTERNS` plus the spec's per-column
overrides): a malformed value is NULL and a cast failure, never silently rounded or reinterpreted.
The CSV is never decompressed to disk; DuckDB reads the gzip stream directly.

SQL safety: every value (paths, null tokens, cast patterns, the timestamp format, the partition
value, COPY options) is a bound parameter (:class:`rl_pipeline.schema.SqlParams`); column names
come from the dataset spec and pass :func:`rl_pipeline.schema.sql_ident` (allowlisted against the
spec's columns) before they reach a statement.

Disk: the per-language ``ORDER BY`` spills to ``<out_root>/.duckdb_tmp-<pid>``. Measured in review
on the full file (memory_limit 1GB, 4 threads, 2026-10-13): **peak spill 1,267,624 KiB
(~1.2 GiB)**, peak ``out_root`` usage 1,380,616 KiB including the staged Parquet. The spill is
capped explicitly (``SET max_temp_directory_size``) at ``min(DuckDBLimits.max_temp_mib, free disk -
output estimate - reserve)``, so a conversion fails cleanly with :class:`InsufficientDiskError`
instead of eating the host's last free space. Work directories of crashed runs (dead pid) are
swept on the next call; a failed run removes its own.
"""

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
from rl_pipeline.datasets import DatasetSpec
from rl_pipeline.hashing import sha256_file
from rl_pipeline.schema import (
    NULL_TOKENS,
    STRICT_PATTERNS,
    SUPPORTED_TYPES,
    TIMESTAMP_FORMAT,
    SqlParams,
    read_header,
    sql_ident,
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
    con.execute("SET memory_limit = $v", {"v": limits.memory_limit})
    con.execute("SET threads = $v", {"v": int(limits.threads)})
    con.execute("SET preserve_insertion_order = true")
    con.execute("SET enable_progress_bar = false")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute("SET temp_directory = $v", {"v": str(temp_dir)})
    if max_temp_bytes is not None:
        con.execute("SET max_temp_directory_size = $v", {"v": f"{int(max_temp_bytes)}B"})
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


def _ident(name: str, spec: DatasetSpec | None = None) -> str:
    """A validated identifier: a plain name and, with ``spec``, one of the spec's columns."""
    return sql_ident(name, spec.names if spec is not None else None)


def _raw_source(csv_path: Path, spec: DatasetSpec, params: SqlParams) -> str:
    """read_csv with no type sniffing: every column VARCHAR, names from the expected schema.

    The path and the ``columns`` struct (name -> 'VARCHAR', in file order) are bound parameters.
    """
    path = params(str(csv_path))
    columns = params(dict.fromkeys(spec.names, "VARCHAR"))
    return (
        f"read_csv({path}, header=true, auto_detect=false, "
        f"delim=',', quote='\"', escape='\"', compression='gzip', columns={columns})"
    )


def _non_missing(name: str, params: SqlParams, null_tokens: tuple[str, ...] = NULL_TOKENS) -> str:
    tokens = ", ".join(params(t) for t in null_tokens)
    raw = _ident(name)
    return f"CASE WHEN {raw} IN ({tokens}) THEN NULL ELSE {raw} END"


def typed_expr(
    name: str,
    duck_type: str,
    *,
    params: SqlParams,
    pattern: str | None = None,
    null_tokens: tuple[str, ...] = NULL_TOKENS,
    timestamp_format: str = TIMESTAMP_FORMAT,
) -> str:
    """SQL turning the raw VARCHAR column into its target type (NULL on failure).

    Strict: a value must fully match ``pattern`` (default: :data:`STRICT_PATTERNS` for its type)
    before it is cast. Overrides only ever *widen the accepted spelling* of an exact value (e.g.
    ``"12.0"`` for an integer), never the value itself. The null tokens, the pattern and the
    timestamp format are bound through ``params`` (the statement's :class:`SqlParams`).
    """
    if duck_type not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported column type {duck_type!r}")
    nn = _non_missing(name, params, null_tokens)
    if duck_type == "VARCHAR":
        return nn
    if duck_type == "BOOLEAN":
        return f"CASE upper({nn}) WHEN 'TRUE' THEN TRUE WHEN 'FALSE' THEN FALSE END"
    guard = f"regexp_full_match({nn}, {params(pattern or STRICT_PATTERNS[duck_type])})"
    if duck_type == "TIMESTAMP":
        return f"CASE WHEN {guard} THEN TRY_STRPTIME({nn}, {params(timestamp_format)}) END"
    return f"CASE WHEN {guard} THEN TRY_CAST({nn} AS {duck_type}) END"


def spec_typed_expr(spec: DatasetSpec, name: str, params: SqlParams) -> str:
    duck_type = spec.types[name]
    return typed_expr(
        name,
        duck_type,
        params=params,
        pattern=spec.pattern_for(name, duck_type),
        null_tokens=spec.null_tokens,
        timestamp_format=spec.timestamp_format,
    )


def _typed_select(spec: DatasetSpec, params: SqlParams) -> str:
    return ", ".join(f"{spec_typed_expr(spec, n, params)} AS {_ident(n, spec)}" for n in spec.names)


# ---------------------------------------------------------------- profiling + gate
def profile_raw(
    con: duckdb.DuckDBPyConnection, csv_path: Path, spec: DatasetSpec
) -> dict[str, Any]:
    """One streaming pass: rows per language, per-column non-missing and cast-failure counts."""
    # Column names come from the spec (DatasetSpec checks its key columns against the schema) and
    # pass _ident's identifier check; every value is a bound parameter of `params`, so the
    # f-strings only ever splice validated identifiers and fixed SQL.
    params = SqlParams()
    aggs: list[str] = ["count(*) AS __rows"]
    for name, duck_type in spec.columns:
        nn = _non_missing(name, params, spec.null_tokens)
        aggs.append(f"count({nn}) AS {_ident('nn__' + name)}")
        if duck_type != "VARCHAR":
            typed = spec_typed_expr(spec, name, params)
            aggs.append(
                f"count(*) FILTER (WHERE ({nn}) IS NOT NULL AND ({typed}) IS NULL) "
                f"AS {_ident('fail__' + name)}"
            )
    lang = _non_missing(spec.partition_column, params, spec.null_tokens)
    source = _raw_source(csv_path, spec, params)
    cur = con.execute(
        f"SELECT {lang} AS __lang, {', '.join(aggs)} FROM {source} "  # noqa: S608  # nosec B608 # identifiers: sql_ident allowlist; values: bound params
        "GROUP BY ALL ORDER BY __lang NULLS FIRST",
        params.values,
    )
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    key_params = SqlParams()
    job_id = spec_typed_expr(spec, spec.job_id_column, key_params)
    build_id = spec_typed_expr(spec, spec.build_id_column, key_params)
    unique = spec_typed_expr(spec, spec.unique_column, key_params)
    key_source = _raw_source(csv_path, spec, key_params)
    key_row = con.execute(
        f"SELECT count({job_id}) - count(DISTINCT {job_id}), "  # noqa: S608  # nosec B608 # identifiers: sql_ident allowlist; values: bound params
        f"count(*) FILTER (WHERE {job_id} IS NULL), "
        f"count(*) FILTER (WHERE {build_id} IS NULL), "
        f"count({unique}) - count(DISTINCT {unique}), "
        f"count(*) FILTER (WHERE {unique} IS NULL) FROM {key_source}",
        key_params.values,
    ).fetchone()
    assert key_row is not None

    columns: dict[str, dict[str, Any]] = {}
    for name, duck_type in spec.columns:
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
        "unique_column": spec.unique_column,
        "duplicate_unique_keys": int(key_row[3]),
        "null_unique_keys": int(key_row[4]),
    }


def quality_gate(
    profile: dict[str, Any], spec: DatasetSpec, max_rate: float = CAST_FAILURE_MAX_RATE
) -> None:
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
            problems.append(f"{spec.partition_column} value {lang!r} is missing or not path-safe")
    job, build, unique = spec.job_id_column, spec.build_id_column, spec.unique_column
    if unique == job:
        if profile["duplicate_job_ids"] or profile["null_job_ids"] or profile["null_build_ids"]:
            problems.append(
                f"key violation: {profile['duplicate_job_ids']} duplicate {job}, "
                f"{profile['null_job_ids']} null {job}, {profile['null_build_ids']} null {build}"
            )
    elif (
        profile["duplicate_unique_keys"]
        or profile["null_unique_keys"]
        or profile["null_job_ids"]
        or profile["null_build_ids"]
    ):
        problems.append(
            f"key violation: {profile['duplicate_unique_keys']} duplicate {unique}, "
            f"{profile['null_unique_keys']} null {unique}, {profile['null_job_ids']} null {job}, "
            f"{profile['null_build_ids']} null {build}"
        )
    if problems:
        raise DataQualityError("data-quality gate failed: " + "; ".join(problems))


# ---------------------------------------------------------------- manifest / skip path
def _settings_block(spec: DatasetSpec) -> dict[str, Any]:
    return {
        "converter_version": CONVERTER_VERSION,
        "duckdb_version": duckdb.__version__,
        "compression": COMPRESSION,
        "row_group_size": ROW_GROUP_SIZE,
        "order_by": list(spec.sort_key),
        "partition_by": spec.partition_column,
        "schema_fingerprint": spec.fingerprint(),
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


def _manifest_still_valid(
    local_dir: Path, version: str, spec: DatasetSpec
) -> tuple[dict[str, Any] | None, str]:
    try:
        manifest = read_manifest(local_dir)
    except (OSError, ValueError) as exc:  # unreadable / not JSON: regenerate, don't crash
        return None, f"unreadable manifest ({exc.__class__.__name__})"
    if manifest is None:
        return None, "no manifest"
    if manifest.get("version") != version:
        return None, "manifest version differs"
    if manifest.get("converter") != _settings_block(spec):
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
    manifest: dict[str, Any], local_dir: Path, spec: DatasetSpec, *, skipped: bool, seconds: float
) -> dict[str, Any]:
    manifest_path = local_dir / MANIFEST_NAME
    return {
        "dataset": spec.key,
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


def _verify_parquet_schema(path: Path, spec: DatasetSpec) -> None:
    schema = pl.read_parquet_schema(path)
    actual = [(n, schema[n]) for n in schema]
    expected = [(n, _POLARS_TYPES[t]) for n, t in spec.columns]
    if actual != expected:
        raise DataQualityError(f"{path} has an unexpected Parquet schema: {actual}")


def _write_partitions(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    staging: Path,
    profile: dict[str, Any],
    spec: DatasetSpec,
) -> list[dict[str, Any]]:
    """One sorted, typed Parquet file per language under ``staging``; verified after writing."""
    files: list[dict[str, Any]] = []
    order = ", ".join(_ident(c, spec) for c in spec.sort_key)
    partition = _ident(spec.partition_column, spec)
    for lang in sorted(profile["rows_per_lang"]):
        rel = f"lang={lang}/{PART_FILENAME}"
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        params = SqlParams()
        select = _typed_select(spec, params)
        source = _raw_source(csv_path, spec, params)
        where = f"{partition} = {params(lang)}"
        options = (
            f"FORMAT parquet, COMPRESSION {params(COMPRESSION)}, "
            f"ROW_GROUP_SIZE {params(ROW_GROUP_SIZE)}"
        )
        con.execute(
            f"COPY (SELECT {select} FROM {source} WHERE {where} ORDER BY {order}) "  # noqa: S608  # nosec B608 # identifiers: sql_ident allowlist; values: bound params
            f"TO {params(str(target))} ({options})",
            params.values,
        )
        row = con.execute(
            "SELECT count(*) FROM read_parquet($path)", {"path": str(target)}
        ).fetchone()
        rows = int(row[0]) if row else -1
        if rows != profile["rows_per_lang"][lang]:
            raise DataQualityError(
                f"{rel}: wrote {rows} rows, expected {profile['rows_per_lang'][lang]}"
            )
        _verify_parquet_schema(target, spec)
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
def _manifest(
    spec: DatasetSpec,
    version: str,
    source: dict[str, Any] | None,
    profile: dict[str, Any],
    files: list[dict[str, Any]],
    max_cast_failure_rate: float,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "dataset": "travistorrent",
        "version": version,
        "source": source or {},
        "converter": _settings_block(spec),
        "schema": [{"name": n, "type": t} for n, t in spec.columns],
        "null_tokens": list(spec.null_tokens),
        "timestamp_format": spec.timestamp_format,
        "timestamp_semantics": spec.timestamp_semantics,
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
    if spec.manifest_extras:
        manifest["dataset_key"] = spec.key
        manifest["column_patterns"] = dict(sorted(spec.patterns.items()))
        manifest["key_profile"] = {
            k: profile[k]
            for k in (
                "unique_column",
                "duplicate_unique_keys",
                "null_unique_keys",
                "duplicate_job_ids",
                "null_job_ids",
                "null_build_ids",
            )
        }
    return manifest


def convert(
    csv_path: Path,
    version: str,
    out_root: Path,
    *,
    dataset: DatasetSpec,
    source: dict[str, Any] | None = None,
    limits: DuckDBLimits | None = None,
    max_cast_failure_rate: float = CAST_FAILURE_MAX_RATE,
) -> dict[str, Any]:
    """Convert ``csv_path`` (laid out as ``dataset``) into ``<out_root>/version=<version>/``."""
    spec = dataset
    if not re.fullmatch(r"[0-9a-f]{32}", version):
        raise ValueError(f"version must be a 32-char lowercase hex md5, got {version!r}")
    started = time.perf_counter()
    local_dir = out_root / f"version={version}"

    manifest, reason = _manifest_still_valid(local_dir, version, spec)
    if manifest is not None:
        log.info(
            "conversion skipped",
            extra={"dataset": spec.key, "local_dir": str(local_dir), "reason": reason},
        )
        return _result(
            manifest, local_dir, spec, skipped=True, seconds=time.perf_counter() - started
        )
    log.info(
        "converting", extra={"dataset": spec.key, "local_dir": str(local_dir), "reason": reason}
    )

    limits = limits or DuckDBLimits()
    out_root.mkdir(parents=True, exist_ok=True)
    sweep_stale_work_dirs(out_root)
    # Output estimate = input size x the spec's measured factor (2017: 207,787,946 B of Parquet
    # for the 264,607,174 B .csv.gz, factor 1.0 is a safe bound).
    output_estimate = int(csv_path.stat().st_size * spec.output_size_factor)
    max_temp = temp_budget(out_root, output_estimate, limits)
    tmp_dir = out_root / f".duckdb_tmp-{os.getpid()}"
    staging = out_root / f".staging-version={version}-{os.getpid()}"
    old = out_root / f".old-version={version}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    log.info("duckdb spill cap", extra={"max_temp_bytes": max_temp, "temp_dir": str(tmp_dir)})
    try:
        con = connect(limits, tmp_dir, max_temp_bytes=max_temp)
        try:
            validate_header(read_header(con, csv_path), spec.names)

            t0 = time.perf_counter()
            profile = profile_raw(con, csv_path, spec)
            profile_seconds = time.perf_counter() - t0
            quality_gate(profile, spec, max_cast_failure_rate)

            t1 = time.perf_counter()
            files = _write_partitions(con, csv_path, staging, profile, spec)
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

        manifest = _manifest(spec, version, source, profile, files, max_cast_failure_rate)
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

    result = _result(
        manifest, local_dir, spec, skipped=False, seconds=time.perf_counter() - started
    )
    result["profile_seconds"] = round(profile_seconds, 3)
    result["write_seconds"] = round(write_seconds, 3)
    log.info(
        "conversion finished",
        extra={k: v for k, v in result.items() if k != "files"},
    )
    return result
