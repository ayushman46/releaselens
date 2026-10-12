"""Public task API (Airflow-agnostic). Every task takes and returns JSON-serialisable dicts.

Chain: ``locate_or_download -> validate_raw_schema -> convert_to_parquet -> load_raw_s3`` plus
``build_data_dictionary``. Each task re-checks what it relies on (md5, header, sha256s), so any
of them can be re-run on its own, and all of them are idempotent by dataset version (= md5).

* ``convert_to_parquet`` accepts the dict from ``locate_or_download`` or ``validate_raw_schema``
  (needs ``path`` and ``version``). Its ``files[].path`` values are **relative to**
  ``local_dir`` (``lang=<lang>/part-00000.parquet``) and map 1:1 onto S3 keys.
* ``load_raw_s3`` accepts the dict returned by ``convert_to_parquet``.
* ``build_data_dictionary`` accepts ``{}`` (locate the version from the dataset), the conversion
  dict, or ``{"local_dir": ..., "markdown_path": ..., "json_path": ...}``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rl_pipeline import convert as _convert
from rl_pipeline import dataset as _dataset
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


def locate_or_download(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Find the verified dataset (download only if missing/corrupt).

    Params: ``dataset_dir`` (default ``RL_DATASET_DIR``), ``allow_download`` (default true).
    Returns ``{"path", "md5", "size", "version", "downloaded"}`` with ``version == md5``.
    """
    params = params or {}
    settings = PipelineSettings()
    dataset_dir = Path(params.get("dataset_dir") or settings.dataset_dir)
    located = _dataset.locate_or_download(
        dataset_dir, allow_download=_as_bool(params.get("allow_download", True), "allow_download")
    )
    return located.as_dict()


def validate_raw_schema(dataset: dict[str, Any]) -> dict[str, Any]:
    """Compare the raw header with the expected 66 names (exact order). Raises on mismatch."""
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
    validate_header(header)
    return {
        "columns": len(header),
        "ok": True,
        "path": str(path),
        "version": str(dataset["version"]),
        "md5": dataset.get("md5", dataset["version"]),
        "size": path.stat().st_size,
    }


def convert_to_parquet(dataset: dict[str, Any]) -> dict[str, Any]:
    """Stream the CSV to typed Parquet under ``<RL_LAKE_DIR>/raw/travistorrent/version=<md5>/``."""
    path = Path(dataset["path"])
    version = str(dataset["version"])
    actual = md5_file(path)
    if actual != version:
        raise _dataset.DatasetCorruptError(f"{path} md5 {actual} != dataset version {version}")
    settings = PipelineSettings()
    source = _dataset.source_as_dict() | {"md5": actual, "size": path.stat().st_size}
    return _convert.convert(
        path,
        version,
        settings.raw_travistorrent_dir,
        source=source,
        limits=_limits(settings),
    )


def load_raw_s3(conversion: dict[str, Any]) -> dict[str, Any]:
    """Upload the converted version to ``s3://<RL_S3_BUCKET_DATA>/raw/travistorrent/...``."""
    settings = PipelineSettings()
    client = _s3.make_client(settings)
    return _s3.load_version(client, settings.s3_bucket_data, conversion)


def build_data_dictionary(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write ``DATA_DICTIONARY.md`` + ``data_dictionary.json`` from the converted Parquet."""
    params = params or {}
    settings = PipelineSettings()
    if params.get("local_dir"):
        local_dir = Path(params["local_dir"])
    else:
        version = params.get("version")
        if not version:
            located = _dataset.locate_or_download(settings.dataset_dir, allow_download=False)
            version = located.version
        local_dir = settings.raw_travistorrent_dir / f"version={version}"
    md_path = Path(params.get("markdown_path") or settings.docs_data_dir / "DATA_DICTIONARY.md")
    json_path = Path(params.get("json_path") or settings.docs_data_dir / "data_dictionary.json")
    built = _dictionary.build_dictionary(local_dir, _limits(settings))
    _dictionary.write_outputs(built, md_path, json_path)
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "version": built["dataset"]["version"],
        "java_builds": built["counts"]["java"]["builds"],
    }


TASKS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "locate_or_download": locate_or_download,
    "validate_raw_schema": validate_raw_schema,
    "convert_to_parquet": convert_to_parquet,
    "load_raw_s3": load_raw_s3,
    "build_data_dictionary": build_data_dictionary,
}
