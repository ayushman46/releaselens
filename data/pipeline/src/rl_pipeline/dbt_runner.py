"""Run the dbt-duckdb project (``data/dbt``) programmatically (dbtRunner), stage by stage (M3a).

Stages (one ``dbt build`` each, tests included; a failing test fails the stage):

=================  =========================================  ==================================
stage              selection                                  materialised as
=================  =========================================  ==================================
``staging``        ``source:travistorrent staging``           tables / views in the DuckDB file
``intermediate``   ``intermediate``                           tables in the DuckDB file
``marts``          ``marts``                                  Parquet: ``marts/<model>/run_id=``
``ml``             ``ml``                                     Parquet: ``marts/<model>/run_id=``
                                                              + ``features/release_features/...``
=================  =========================================  ==================================

``--indirect-selection buildable`` keeps cross-layer tests (e.g. ``fct_builds`` vs
``stg_travis_builds``) in the stage that builds their last parent.

Environment (set for the duration of each invocation, restored afterwards): ``RL_LAKE_DIR``
(DuckDB file and dbt work dir ``<lake>/dbt``), ``RL_S3_*`` from :class:`PipelineSettings`,
``RL_DBT_TARGET``, ``RL_DBT_MEMORY_LIMIT`` (= ``RL_DUCKDB_MEMORY_LIMIT``),
``RL_DBT_MAX_TEMP_SIZE`` (spill cap: ``min(RL_DUCKDB_MAX_TEMP_MIB, free disk - reserve)``, the
same guard as the conversion), target/log paths under the work dir (the project directory may be
read-only, e.g. in the Airflow image), and dbt's anonymous usage stats off.

Vars: ``dataset_version`` (md5 of the primary file), ``run_id`` (path-safe), ``raw_root`` /
``output_root`` (``s3://<bucket>/...``; local directories for the ``fixture`` target) and
``feature_schema_version`` (from the installed rl_core).

Only the primary dataset has dbt models (ADR-0017); any other dataset raises
:class:`DbtNotApplicableError` (the DAG turns it into a skip).
"""

# ruff: noqa: S608 - SQL is assembled from model names and locations through sql_literal()

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import duckdb

from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from rl_pipeline import datasets as _datasets
from rl_pipeline import s3 as _s3
from rl_pipeline.convert import DuckDBLimits, temp_budget
from rl_pipeline.schema import sql_literal
from rl_pipeline.settings import PipelineSettings

log = logging.getLogger("rl_pipeline.dbt")

Stage = Literal["staging", "intermediate", "marts", "ml"]
STAGES: Final[tuple[Stage, ...]] = ("staging", "intermediate", "marts", "ml")
SELECTORS: Final[Mapping[Stage, tuple[str, ...]]] = {
    "staging": ("source:travistorrent", "staging"),
    "intermediate": ("intermediate",),
    "marts": ("marts",),
    "ml": ("ml",),
}
TARGETS: Final = ("dev", "fixture")
FEATURES_MODEL: Final = "mart_build_features"
SPLIT_MODEL: Final = "split_cutpoints"
PART_FILENAME: Final = "part-00000.parquet"
DUCKDB_FILES: Final[Mapping[str, str]] = {
    "dev": "releaselens.duckdb",
    "fixture": "releaselens_fixture.duckdb",
}
# Upper bound for what one run adds to the work dir (DuckDB file growth, target/, logs): the real
# run measured well below this (docs/data/PIPELINE.md §8); used only for the spill budget.
WORK_DIR_ESTIMATE_BYTES: Final = 256 * 1024 * 1024
# dbt vars a caller may override (``params["vars"]``); everything else is derived.
OVERRIDABLE_VARS: Final = frozenset(
    {"label_rate_min", "label_rate_max", "split_train_fraction", "split_validation_fraction"}
)
_RUN_ID_RE: Final = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# Exit code of a DAG task for which dbt does not apply (Airflow: skip_on_exit_code -> skipped).
SKIP_EXIT_CODE: Final = 99


class DbtNotApplicableError(ValueError):
    """The dbt project models the primary dataset only (ADR-0017)."""


@contextlib.contextmanager
def skip_if_not_applicable() -> Iterator[None]:
    """For DAG task bodies: :class:`DbtNotApplicableError` -> log + ``SystemExit(SKIP_EXIT_CODE)``.

    The external-python boundary only reports an exit code; the DAG maps this one to "skipped"
    (``skip_on_exit_code``), so a secondary-dataset run is neither a failure nor a fake success.
    """
    try:
        yield
    except DbtNotApplicableError as exc:
        log.warning("dbt skipped", extra={"reason": str(exc)})
        raise SystemExit(SKIP_EXIT_CODE) from exc


class DbtStageError(RuntimeError):
    """``dbt`` reported a failure (model error or failing test); the summary lists the nodes."""

    def __init__(self, message: str, summary: dict[str, Any]) -> None:
        super().__init__(message)
        self.summary = summary


def sanitize_run_id(run_id: str | None) -> str:
    """A path-safe run id: Airflow's ``manual__2026-10-13T03:00:00+00:00`` -> ``manual__2026-...``.

    Runs of characters outside ``[A-Za-z0-9._-]`` become one ``_``; leading dots/dashes and
    underscores are dropped; at most 128 characters. Empty -> ``manual``.
    """
    cleaned = _RUN_ID_RE.sub("_", (run_id or "").strip()).lstrip("._-")[:128]
    return cleaned or "manual"


def _require_safe(value: str, name: str) -> str:
    if not _SAFE_SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{name} {value!r} is not a safe path segment")
    return value


@dataclass(frozen=True, slots=True)
class DbtContext:
    """Everything one dbt invocation needs (resolved once per task call)."""

    project_dir: Path
    work_dir: Path
    target: str
    dataset: str
    dataset_version: str
    run_id: str
    raw_root: str
    output_root: str
    settings: PipelineSettings = field(repr=False)
    extra_vars: Mapping[str, float] = field(default_factory=dict)

    @property
    def duckdb_path(self) -> Path:
        return self.work_dir / DUCKDB_FILES[self.target]

    @property
    def vars(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "run_id": self.run_id,
            "raw_root": self.raw_root,
            "output_root": self.output_root,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            **self.extra_vars,
        }

    @property
    def is_s3(self) -> bool:
        return self.output_root.startswith("s3://")

    def output_location(self, model: str) -> str:
        """Mirror of ``macros/external_location.sql`` (the fixture test checks they agree)."""
        root = self.output_root.rstrip("/")
        if model == FEATURES_MODEL:
            return (
                f"{root}/features/release_features/schema={FEATURE_SCHEMA_VERSION}/"
                f"run_id={self.run_id}/{PART_FILENAME}"
            )
        return f"{root}/marts/{model}/run_id={self.run_id}/{PART_FILENAME}"

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.dataset_version,
            "run_id": self.run_id,
            "target": self.target,
            "raw_root": self.raw_root,
            "output_root": self.output_root,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "duckdb_path": str(self.duckdb_path),
            "vars": dict(self.extra_vars),
        }


def make_context(
    params: Mapping[str, Any] | None, settings: PipelineSettings | None = None
) -> DbtContext:
    """Resolve dataset, version, run id, target and roots from task params + settings.

    Params: ``dataset`` / ``version`` (default: the primary and its registered md5), ``run_id``
    (default ``manual``; sanitised), ``target`` (``dev`` | ``fixture``), ``raw_root`` and
    ``output_root`` (default ``s3://<RL_S3_BUCKET_DATA>/raw/travistorrent`` and
    ``s3://<RL_S3_BUCKET_DATA>``; required for ``fixture``), ``project_dir`` (default
    ``RL_DBT_PROJECT_DIR``), ``vars`` (overrides of :data:`OVERRIDABLE_VARS`, e.g. the label-rate
    bounds). A previous dbt task's result carries all of these forward.
    """
    params = dict(params or {})
    settings = settings or PipelineSettings()
    spec = _datasets.resolve(params)
    if spec.key != _datasets.PRIMARY:
        raise DbtNotApplicableError(
            f"dbt models are defined for the primary dataset {_datasets.PRIMARY!r} only "
            f"(ADR-0017); got {spec.key!r}"
        )
    version = str(params.get("version") or spec.source.md5)
    target = str(params.get("target") or "dev")
    if target not in TARGETS:
        raise ValueError(f"unknown dbt target {target!r}; expected one of {TARGETS}")
    if target == "dev" and version != spec.source.md5:
        raise ValueError(f"version {version} is not the primary dataset's md5 {spec.source.md5}")
    if not re.fullmatch(r"[0-9a-f]{32}", version):
        raise ValueError(f"dataset version must be an md5 hex digest, got {version!r}")
    bucket = settings.s3_bucket_data
    raw_root = str(params.get("raw_root") or f"s3://{bucket}/{_s3.RAW_PREFIX}")
    output_root = str(params.get("output_root") or f"s3://{bucket}")
    if target == "fixture" and (raw_root.startswith("s3://") or output_root.startswith("s3://")):
        raise ValueError("the fixture target reads and writes local directories only")
    for root in (raw_root, output_root):
        if "'" in root or "{" in root:
            raise ValueError(f"unsupported character in root {root!r}")
    extra = dict(params.get("vars") or {})
    unknown = sorted(set(extra) - OVERRIDABLE_VARS)
    if unknown:
        allowed = sorted(OVERRIDABLE_VARS)
        raise ValueError(f"vars {unknown} cannot be overridden; allowed: {allowed}")
    extra_vars = {k: float(v) for k, v in extra.items()}
    project_dir = Path(params.get("project_dir") or settings.dbt_project_dir).resolve()
    if not (project_dir / "dbt_project.yml").is_file():
        raise FileNotFoundError(f"no dbt_project.yml in {project_dir}")
    return DbtContext(
        project_dir=project_dir,
        work_dir=(settings.lake_dir / "dbt").resolve(),
        target=target,
        dataset=spec.key,
        dataset_version=version,
        run_id=_require_safe(sanitize_run_id(params.get("run_id")), "run_id"),
        raw_root=raw_root,
        output_root=output_root,
        settings=settings,
        extra_vars=extra_vars,
    )


def _env(ctx: DbtContext, max_temp_bytes: int) -> dict[str, str]:
    s = ctx.settings
    return {
        "RL_LAKE_DIR": str(ctx.work_dir.parent),
        "RL_DBT_TARGET": ctx.target,
        "RL_DBT_MEMORY_LIMIT": s.duckdb_memory_limit,
        "RL_DBT_MAX_TEMP_SIZE": f"{int(max_temp_bytes)}B",
        "RL_S3_ENDPOINT": s.s3_endpoint,
        "RL_S3_ACCESS_KEY": s.s3_access_key,
        "RL_S3_SECRET_KEY": s.s3_secret_key.get_secret_value(),
        "RL_S3_REGION": s.s3_region,
        "RL_S3_BUCKET_DATA": s.s3_bucket_data,
        "RL_DBT_PACKAGES_DIR": str(ctx.work_dir / "dbt_packages"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DO_NOT_TRACK": "1",
        "DBT_USE_COLORS": "false",
    }


@contextlib.contextmanager
def patched_environ(values: Mapping[str, str]) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _node_summary(result: Any) -> dict[str, Any]:
    node = result.node
    return {
        "unique_id": node.unique_id,
        "name": node.name,
        "resource_type": str(node.resource_type),
        "status": str(result.status),
        "execution_time_s": round(float(result.execution_time or 0.0), 3),
        "failures": result.failures,
        "message": result.message,
    }


def _prepare_local_outputs(ctx: DbtContext, models: list[str]) -> None:
    """DuckDB's COPY does not create directories: make them for local (fixture) output roots."""
    for model in models:
        Path(ctx.output_location(model)).parent.mkdir(parents=True, exist_ok=True)


def _common_args(ctx: DbtContext, command: str) -> list[str]:
    """Project/profile/log locations (+ target, target path and vars where the command takes them:
    ``dbt deps`` accepts neither ``--target`` nor ``--target-path``)."""
    argv = [
        "--project-dir",
        str(ctx.project_dir),
        "--profiles-dir",
        str(ctx.project_dir),
        "--log-path",
        str(ctx.work_dir / "logs"),
        "--vars",
        json.dumps(ctx.vars, sort_keys=True),
    ]
    if command != "deps":
        argv += ["--target", ctx.target, "--target-path", str(ctx.work_dir / "target")]
    return argv


def invoke(ctx: DbtContext, command: str, *args: str) -> dict[str, Any]:
    """Run one dbt command; return a JSON-safe summary or raise :class:`DbtStageError`."""
    from dbt.cli.main import dbtRunner  # heavy import, only where dbt actually runs

    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    limits = DuckDBLimits(
        ctx.settings.duckdb_memory_limit,
        ctx.settings.duckdb_threads,
        ctx.settings.duckdb_max_temp_mib,
    )
    max_temp = temp_budget(ctx.work_dir, WORK_DIR_ESTIMATE_BYTES, limits)
    argv = [command, *_common_args(ctx, command), *args]
    log.info(
        "dbt invoke",
        extra={"command": command, "dbt_args": list(args), "max_temp_bytes": max_temp, **ctx.vars},
    )
    started = time.perf_counter()
    with patched_environ(_env(ctx, max_temp)):
        res = dbtRunner().invoke(argv)
    elapsed = round(time.perf_counter() - started, 3)
    shutil.rmtree(ctx.work_dir / ".duckdb_tmp", ignore_errors=True)
    nodes = [_node_summary(r) for r in getattr(res.result, "results", None) or []]
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    summary: dict[str, Any] = {
        "command": command,
        "args": list(args),
        "success": bool(res.success),
        "elapsed_s": elapsed,
        "status_counts": dict(sorted(counts.items())),
        "nodes": nodes,
    }
    if not res.success:
        bad = [n for n in nodes if n["status"] not in {"success", "pass", "skipped"}]
        detail = "; ".join(f"{n['name']}: {n['status']} ({n['message']})" for n in bad[:10])
        reason = detail or repr(res.exception)
        raise DbtStageError(f"dbt {command} failed: {reason}", summary)
    return summary


def deps(ctx: DbtContext) -> dict[str, Any]:
    """``dbt deps`` (the project has no packages today; kept so adding one needs no DAG change)."""
    return {**ctx.summary(), "dbt": invoke(ctx, "deps")}


def _models_in(summary: dict[str, Any]) -> list[str]:
    return [n["name"] for n in summary["nodes"] if n["resource_type"] == "model"]


def build_stage(ctx: DbtContext, stage: Stage) -> dict[str, Any]:
    """``dbt build`` for one stage, then measure what it produced (rows, bytes, sha256)."""
    if stage not in SELECTORS:
        raise ValueError(f"unknown stage {stage!r}")
    if stage in ("marts", "ml") and not ctx.is_s3:
        _prepare_local_outputs(ctx, _list_models(ctx, stage))
    result = invoke(
        ctx,
        "build",
        "--select",
        *SELECTORS[stage],
        "--indirect-selection",
        "buildable",
    )
    models = _models_in(result)
    return {
        **ctx.summary(),
        "stage": stage,
        "dbt": result,
        "outputs": measure_outputs(ctx, models, external=stage in ("marts", "ml")),
    }


def _list_models(ctx: DbtContext, stage: Stage) -> list[str]:
    """Model names of a stage (``dbt ls``), used to pre-create local output directories."""
    from dbt.cli.main import dbtRunner

    argv = [
        "ls",
        *_common_args(ctx, "ls"),
        "--resource-type",
        "model",
        "--select",
        *SELECTORS[stage],
        "--output",
        "name",
        "--quiet",
    ]
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    with patched_environ(_env(ctx, 64 * 1024 * 1024)):
        res = dbtRunner().invoke(argv)
    if not res.success or not isinstance(res.result, list):
        raise DbtStageError(f"dbt ls failed: {res.exception!r}", {"command": "ls"})
    return sorted(str(name) for name in res.result)


def _connect(ctx: DbtContext) -> duckdb.DuckDBPyConnection:
    """A read connection with the profile's settings (+ httpfs and the S3 secret for ``dev``)."""
    # Same (default) configuration as dbt-duckdb's own connection: DuckDB shares one database
    # instance per file and process and refuses a second one with a different access mode.
    con = duckdb.connect(str(ctx.duckdb_path))
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"SET memory_limit = {sql_literal(ctx.settings.duckdb_memory_limit)}")
    if ctx.target == "dev":
        s = ctx.settings
        endpoint = re.sub(r"^https?://", "", s.s3_endpoint)
        con.execute("LOAD httpfs")
        con.execute(
            "CREATE OR REPLACE TEMPORARY SECRET rl_s3_measure (TYPE s3, "
            f"KEY_ID {sql_literal(s.s3_access_key)}, "
            f"SECRET {sql_literal(s.s3_secret_key.get_secret_value())}, "
            f"REGION {sql_literal(s.s3_region)}, ENDPOINT {sql_literal(endpoint)}, "
            f"URL_STYLE 'path', USE_SSL {'true' if s.s3_endpoint.startswith('https') else 'false'})"
        )
    return con


def _parquet(location: str) -> str:
    """``read_parquet`` of one output file; ``run_id=``/``schema=`` path segments are no columns."""
    return f"read_parquet({sql_literal(location)}, hive_partitioning = false)"


def _sha256_and_size(ctx: DbtContext, location: str) -> tuple[str, int]:
    if location.startswith("s3://"):
        bucket, key = location[len("s3://") :].split("/", 1)
        body = _s3.make_client(ctx.settings).get_object(Bucket=bucket, Key=key)["Body"]
        digest, size = hashlib.sha256(), 0
        for chunk in iter(lambda: body.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size
    path = Path(location)
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def measure_outputs(ctx: DbtContext, models: list[str], *, external: bool) -> list[dict[str, Any]]:
    """Row counts per model; for external models also location, bytes and sha256 of the file."""
    out: list[dict[str, Any]] = []
    con = _connect(ctx)
    try:
        for model in sorted(models):
            item: dict[str, Any] = {"model": model}
            if external:
                location = ctx.output_location(model)
                row = con.execute(f"SELECT count(*) FROM {_parquet(location)}").fetchone()
                sha, size = _sha256_and_size(ctx, location)
                item |= {"location": location, "rows": int(row[0]) if row else -1}
                item |= {"bytes": size, "sha256": sha}
            else:
                row = con.execute(f'SELECT count(*) FROM main."{model}"').fetchone()
                item["rows"] = int(row[0]) if row else -1
            out.append(item)
    finally:
        con.close()
    return out


def feature_snapshot_report(ctx: DbtContext) -> dict[str, Any]:
    """Cut points, rows and label balance per split of the feature snapshot just written."""
    features = ctx.output_location(FEATURES_MODEL)
    cuts = ctx.output_location(SPLIT_MODEL)
    con = _connect(ctx)
    try:
        cut_row = con.execute(
            "SELECT n_labelled_builds, strftime(first_started_at, '%Y-%m-%dT%H:%M:%SZ'), "
            "strftime(validation_start_at, '%Y-%m-%dT%H:%M:%SZ'), "
            "strftime(test_start_at, '%Y-%m-%dT%H:%M:%SZ'), "
            "strftime(last_started_at, '%Y-%m-%dT%H:%M:%SZ'), n_train, n_validation, n_test "
            f"FROM {_parquet(cuts)}"
        ).fetchone()
        splits = con.execute(
            "SELECT split, count(*), sum(label), "
            "strftime(min(started_at), '%Y-%m-%dT%H:%M:%SZ'), "
            "strftime(max(started_at), '%Y-%m-%dT%H:%M:%SZ') "
            f"FROM {_parquet(features)} GROUP BY split ORDER BY split"
        ).fetchall()
        audit = con.execute(
            "SELECT count(*) FILTER (WHERE max_history_input_finished_at IS NOT NULL), "
            "count(*) FILTER (WHERE max_history_input_finished_at >= epoch_us(started_at) / 1e6) "
            f"FROM {_parquet(features)}"
        ).fetchone()
        columns = [
            r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {_parquet(features)}").fetchall()
        ]
    finally:
        con.close()
    if cut_row is None or audit is None:
        raise RuntimeError("feature snapshot outputs are empty")
    per_split = {
        name: {
            "rows": int(n),
            "positives": int(pos or 0),
            "label_rate": round(float(pos or 0) / n, 6) if n else None,
            "first_started_at": first,
            "last_started_at": last,
        }
        for name, n, pos, first, last in splits
    }
    total = sum(v["rows"] for v in per_split.values())
    positives = sum(v["positives"] for v in per_split.values())
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "features_location": features,
        "n_features": len(ALL_FEATURE_NAMES),
        "columns": columns,
        "cutpoints": {
            "n_labelled_builds": int(cut_row[0]),
            "first_started_at": cut_row[1],
            "validation_start_at": cut_row[2],
            "test_start_at": cut_row[3],
            "last_started_at": cut_row[4],
            "n_train": int(cut_row[5]),
            "n_validation": int(cut_row[6]),
            "n_test": int(cut_row[7]),
        },
        "splits": per_split,
        "rows": total,
        "label_rate": round(positives / total, 6) if total else None,
        "rows_with_history": int(audit[0]),
        "history_after_start_violations": int(audit[1]),
    }


def write_feature_manifest(
    ctx: DbtContext, report: dict[str, Any], outputs: list[dict[str, Any]]
) -> str:
    """``_manifest.json`` next to the features file: deterministic (no timings, no wall clock)."""
    features = next(o for o in outputs if o["model"] == FEATURES_MODEL)
    manifest = {
        "dataset": ctx.dataset,
        "dataset_version": ctx.dataset_version,
        "run_id": ctx.run_id,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(ALL_FEATURE_NAMES),
        "file": {
            "path": PART_FILENAME,
            "rows": features["rows"],
            "bytes": features["bytes"],
            "sha256": features["sha256"],
        },
        "cutpoints": report["cutpoints"],
        "splits": report["splits"],
        "label_rate": report["label_rate"],
        "inputs": {
            o["model"]: {"location": o["location"], "sha256": o["sha256"], "rows": o["rows"]}
            for o in outputs
            if o["model"] != FEATURES_MODEL
        },
    }
    body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    location = ctx.output_location(FEATURES_MODEL).rsplit("/", 1)[0] + "/_manifest.json"
    if location.startswith("s3://"):
        bucket, key = location[len("s3://") :].split("/", 1)
        _s3.make_client(ctx.settings).put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata={"sha256": hashlib.sha256(body).hexdigest()},
        )
    else:
        Path(location).write_bytes(body)
    return location


def prepare_fixture(work_root: Path, limits: DuckDBLimits | None = None) -> dict[str, Any]:
    """Convert the SYNTHETIC 2016 fixture into ``<work_root>/raw`` for the ``fixture`` target.

    The CSV is regenerated by :func:`rl_pipeline.testing.build_rows_2016` (byte-identical to the
    committed ``travistorrent_5_3_2016_synthetic_fixture.csv.gz``), converted by the real
    converter, and the returned params point dbt at it: ``raw_root`` = the local raw dir,
    ``output_root`` = ``<work_root>/out``. Never real data.
    """
    from rl_pipeline.convert import convert
    from rl_pipeline.hashing import md5_file
    from rl_pipeline.schema import TT_5_3_2016_NAMES
    from rl_pipeline.testing import FIXTURE_2016_NAME, build_rows_2016, write_csv_gz

    work_root = work_root.resolve()
    csv_path = write_csv_gz(
        work_root / "input" / FIXTURE_2016_NAME, build_rows_2016(), list(TT_5_3_2016_NAMES)
    )
    version = md5_file(csv_path)
    raw_dir = work_root / "raw" / "travistorrent"
    convert(
        csv_path,
        version,
        raw_dir,
        dataset=_datasets.TT_5_3_2016,
        limits=limits or DuckDBLimits("256MB", 2),
    )
    return {
        "dataset": _datasets.PRIMARY,
        "version": version,
        "target": "fixture",
        "raw_root": str(raw_dir),
        "output_root": str(work_root / "out"),
    }
