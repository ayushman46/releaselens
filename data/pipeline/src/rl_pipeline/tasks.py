"""Public task API (Airflow-agnostic). Every task takes and returns JSON-serialisable dicts.

Chain: ``locate_or_download -> validate_raw_schema -> convert_to_parquet -> load_raw_s3`` plus
``build_data_dictionary`` and ``list_datasets``. Each task re-checks what it relies on (md5,
header, sha256s), so any of them can be re-run on its own, and all of them are idempotent by
dataset version (= md5).

dbt (M3a, :mod:`rl_pipeline.dbt_runner`): ``dbt_deps -> dbt_build_staging ->
dbt_build_intermediate -> dbt_build_marts -> build_feature_snapshot`` (``dbt_build_all`` runs
them in order). Each accepts the previous task's dict (or ``{"dataset", "version", "run_id",
"target", "raw_root", "output_root"}``) and returns the context plus dbt's per-node results and
the measured outputs. A failing model or test raises. Only the primary dataset has dbt models
(:class:`~rl_pipeline.dbt_runner.DbtNotApplicableError` otherwise).

**Dataset selection.** Every task accepts ``{"dataset": <key>}`` (see
:mod:`rl_pipeline.datasets`; default: the primary ``travistorrent-5-3-2016``). Results carry the
key forward, so the next task in the chain uses the same spec. Without a key, a ``version`` equal
to a registered md5 selects that release; anything else falls back to the default.

* ``locate_or_download`` looks in ``<RL_DATASETS_DIR>/<spec.subdir>`` unless ``dataset_dir`` is
  given, and verifies the spec's publisher md5.
* ``convert_to_parquet`` accepts the dict from ``locate_or_download`` or ``validate_raw_schema``
  (needs ``path`` and ``version``). Its ``files[].path`` values are **relative to**
  ``local_dir`` (``lang=<lang>/part-00000.parquet``) and map 1:1 onto S3 keys.
* ``load_raw_s3`` accepts the dict returned by ``convert_to_parquet``.
* ``build_data_dictionary`` accepts ``{"dataset": ...}`` (locate the version from the dataset),
  the conversion dict, or ``{"local_dir": ..., "markdown_path": ..., "json_path": ...}``; with
  ``crosscheck_dir`` (the monperrus archive) it adds the UTC/status cross-check.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rl_pipeline import convert as _convert
from rl_pipeline import dataset as _dataset
from rl_pipeline import datasets as _datasets
from rl_pipeline import dbt_runner as _dbt
from rl_pipeline import dictionary as _dictionary
from rl_pipeline import s3 as _s3
from rl_pipeline.hashing import md5_file
from rl_pipeline.schema import read_header, validate_header
from rl_pipeline.settings import PipelineSettings

log = logging.getLogger("rl_pipeline.tasks")


def _limits(settings: PipelineSettings) -> _convert.DuckDBLimits:
    return _convert.DuckDBLimits(
        settings.duckdb_memory_limit, settings.duckdb_threads, settings.duckdb_max_temp_mib
    )


def _as_bool(value: Any, name: str) -> bool:
    """JSON bools, or the strings "true"/"false" (CLI/templating); anything else is an error."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{name} must be a boolean, got {value!r}")


def list_datasets(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The registered dataset specs (keys, files, md5s) and the default key."""
    del params
    return {
        "default": _datasets.DEFAULT_DATASET,
        "datasets": [spec.summary() for spec in _datasets.DATASETS.values()],
    }


def locate_or_download(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Find the verified dataset (download only if missing/corrupt).

    Params: ``dataset`` (default primary), ``dataset_dir`` (default
    ``<RL_DATASETS_DIR>/<spec.subdir>``), ``allow_download`` (default true).
    Returns ``{"dataset", "path", "md5", "size", "version", "downloaded"}`` with ``version == md5``.
    """
    params = params or {}
    spec = _datasets.get_dataset(params.get("dataset"))
    settings = PipelineSettings()
    dataset_dir = Path(params.get("dataset_dir") or settings.dataset_dir_for(spec.subdir))
    located = _dataset.locate_or_download(
        dataset_dir,
        spec.source,
        allow_download=_as_bool(params.get("allow_download", True), "allow_download"),
    )
    return {"dataset": spec.key, **located.as_dict()}


def validate_raw_schema(dataset: dict[str, Any]) -> dict[str, Any]:
    """Compare the raw header with the spec's expected names (exact order). Raises on mismatch."""
    spec = _datasets.resolve(dataset)
    path = Path(dataset["path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    expected_size = dataset.get("size")
    if expected_size is not None and path.stat().st_size != int(expected_size):
        raise _dataset.DatasetCorruptError(
            f"{path} is {path.stat().st_size} bytes, expected {expected_size}"
        )
    settings = PipelineSettings()
    con = _convert.connect(_limits(settings))
    try:
        header = read_header(con, path)
    finally:
        con.close()
    validate_header(header, spec.names)
    return {
        "dataset": spec.key,
        "columns": len(header),
        "ok": True,
        "path": str(path),
        "version": str(dataset["version"]),
        "md5": dataset.get("md5", dataset["version"]),
        "size": path.stat().st_size,
    }


def convert_to_parquet(dataset: dict[str, Any]) -> dict[str, Any]:
    """Stream the CSV to typed Parquet under ``<RL_LAKE_DIR>/raw/travistorrent/version=<md5>/``."""
    spec = _datasets.resolve(dataset)
    path = Path(dataset["path"])
    version = str(dataset["version"])
    actual = md5_file(path)
    if actual != version:
        raise _dataset.DatasetCorruptError(f"{path} md5 {actual} != dataset version {version}")
    settings = PipelineSettings()
    source = _dataset.source_as_dict(spec.source) | {"md5": actual, "size": path.stat().st_size}
    return _convert.convert(
        path,
        version,
        settings.raw_travistorrent_dir,
        dataset=spec,
        source=source,
        limits=_limits(settings),
    )


def load_raw_s3(conversion: dict[str, Any]) -> dict[str, Any]:
    """Upload the converted version to ``s3://<RL_S3_BUCKET_DATA>/raw/travistorrent/...``."""
    spec = _datasets.resolve(conversion)
    settings = PipelineSettings()
    client = _s3.make_client(settings)
    loaded = _s3.load_version(client, settings.s3_bucket_data, conversion)
    return {"dataset": spec.key, "version": str(conversion["version"]), **loaded}


def build_data_dictionary(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the spec's data dictionary (Markdown + JSON twin) from the converted Parquet."""
    params = params or {}
    settings = PipelineSettings()
    if params.get("local_dir") and not params.get("dataset"):
        # Identify the release from the converted version itself (key, md5 or schema).
        manifest = _convert.read_manifest(Path(params["local_dir"])) or {}
        names = tuple(str(c.get("name")) for c in manifest.get("schema", []))
        spec = (
            _datasets.resolve(
                {"dataset": manifest.get("dataset_key"), "version": manifest.get("version")}
            )
            if manifest.get("dataset_key") or _datasets.dataset_for_version(manifest.get("version"))
            else _datasets.dataset_for_schema(names) or _datasets.get_dataset(None)
        )
    else:
        spec = _datasets.resolve(params)
    if params.get("local_dir"):
        local_dir = Path(params["local_dir"])
    else:
        version = params.get("version")
        if not version:
            located = _dataset.locate_or_download(
                settings.dataset_dir_for(spec.subdir), spec.source, allow_download=False
            )
            version = located.version
        local_dir = settings.raw_travistorrent_dir / f"version={version}"
    md_path = Path(params.get("markdown_path") or settings.docs_data_dir / spec.dictionary_markdown)
    json_path = Path(params.get("json_path") or settings.docs_data_dir / spec.dictionary_json)
    crosscheck = params.get("crosscheck_dir")
    built = _dictionary.build_dictionary(
        local_dir,
        spec,
        _limits(settings),
        crosscheck_dir=Path(crosscheck).expanduser() if crosscheck else None,
    )
    _dictionary.write_outputs(built, md_path, json_path)
    return {
        "dataset": spec.key,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "version": built["dataset"]["version"],
        "java_builds": built["counts"]["java"]["builds"],
    }


def dbt_deps(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """``dbt deps`` for the project (no packages today)."""
    return _dbt.deps(_dbt.make_context(params))


def dbt_build_staging(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Source tests + staging models (tables in the DuckDB file) + their tests."""
    return _dbt.build_stage(_dbt.make_context(params), "staging")


def dbt_build_intermediate(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Intermediate models (naive history, change profile, timing) + their tests."""
    return _dbt.build_stage(_dbt.make_context(params), "intermediate")


def dbt_build_marts(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analytics marts written as Parquet under ``marts/<model>/run_id=<run_id>/`` + tests."""
    return _dbt.build_stage(_dbt.make_context(params), "marts")


def build_feature_snapshot(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """ML models (inputs, cut points, rl_core features) + tests, then the snapshot manifest.

    The features land in ``features/release_features/schema=<v>/run_id=<run_id>/`` with a
    deterministic ``_manifest.json`` (file sha256, cut points, rows and label balance per split).
    """
    ctx = _dbt.make_context(params)
    built = _dbt.build_stage(ctx, "ml")
    report = _dbt.feature_snapshot_report(ctx)
    if report["history_after_start_violations"]:
        raise RuntimeError(
            f"{report['history_after_start_violations']} rows use history that had not finished "
            "before their start (the dbt test should have failed first)"
        )
    manifest = _dbt.write_feature_manifest(ctx, report, built["outputs"])
    return {**built, "feature_snapshot": report, "manifest_location": manifest}


def dbt_build_all(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """deps + the four stages in DAG order (``make dbt-build``); stops at the first failure."""
    ctx_params = dict(params or {})
    started = time.perf_counter()
    steps: dict[str, Any] = {"dbt_deps": dbt_deps(ctx_params)}
    previous: dict[str, Any] = ctx_params
    for name, task in (
        ("dbt_build_staging", dbt_build_staging),
        ("dbt_build_intermediate", dbt_build_intermediate),
        ("dbt_build_marts", dbt_build_marts),
        ("build_feature_snapshot", build_feature_snapshot),
    ):
        previous = task({**previous, **ctx_params})
        steps[name] = previous
    ctx = _dbt.make_context(ctx_params)
    return {**ctx.summary(), "elapsed_s": round(time.perf_counter() - started, 3), "steps": steps}


def dbt_build_fixture(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """``dbt_build_all`` on the SYNTHETIC 2016 fixture, without S3 (CI, ``make dbt-fixture``).

    Params: ``work_dir`` (default ``<RL_LAKE_DIR>/dbt-fixture``; recreated), ``run_id``. The
    fixture is converted with the real converter into ``<work_dir>/raw``; dbt's DuckDB file,
    target/ and logs/ go to ``<work_dir>/dbt`` and the Parquet outputs to ``<work_dir>/out``.
    """
    params = dict(params or {})
    settings = PipelineSettings()
    work_dir = Path(params.get("work_dir") or settings.lake_dir / "dbt-fixture").resolve()
    if work_dir.exists():
        shutil.rmtree(work_dir)
    fixture = _dbt.prepare_fixture(work_dir, _limits(settings))
    with _dbt.patched_environ({"RL_LAKE_DIR": str(work_dir)}):
        result = dbt_build_all({**fixture, "run_id": params.get("run_id") or "fixture"})
    return {"work_dir": str(work_dir), **result}


TASKS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "list_datasets": list_datasets,
    "locate_or_download": locate_or_download,
    "validate_raw_schema": validate_raw_schema,
    "convert_to_parquet": convert_to_parquet,
    "load_raw_s3": load_raw_s3,
    "build_data_dictionary": build_data_dictionary,
    "dbt_deps": dbt_deps,
    "dbt_build_staging": dbt_build_staging,
    "dbt_build_intermediate": dbt_build_intermediate,
    "dbt_build_marts": dbt_build_marts,
    "build_feature_snapshot": build_feature_snapshot,
    "dbt_build_all": dbt_build_all,
    "dbt_build_fixture": dbt_build_fixture,
}
