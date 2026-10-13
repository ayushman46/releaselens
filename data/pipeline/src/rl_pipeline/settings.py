"""Pipeline configuration from ``RL_*`` environment variables (pydantic-settings).

| Variable | Default |
|---|---|
| ``RL_DATASETS_DIR`` | ``<repo>/data/lake/downloads`` (one sub-directory per dataset spec) |
| ``RL_LAKE_DIR`` | ``<repo>/data/lake`` |
| ``RL_DOCS_DATA_DIR`` | ``<repo>/docs/data`` |
| ``RL_DBT_PROJECT_DIR`` | ``<repo>/data/dbt`` (the Airflow image: ``/opt/airflow/dbt``) |
| ``RL_S3_ENDPOINT`` | ``http://localhost:29000`` |
| ``RL_S3_ACCESS_KEY`` / ``RL_S3_SECRET_KEY`` | ``rlens`` / ``rlens-dev-secret`` (dev/test only) |
| ``RL_S3_REGION`` | ``us-east-1`` |
| ``RL_S3_BUCKET_DATA`` / ``RL_S3_BUCKET_MLFLOW`` | ``releaselens-data`` / ``releaselens-mlflow`` |
| ``RL_DUCKDB_MEMORY_LIMIT`` / ``RL_DUCKDB_THREADS`` | ``1GB`` / ``4`` |
| ``RL_DUCKDB_MAX_TEMP_MIB`` | ``3072`` (spill cap; further limited by free disk) |
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "rlens-dev-secret"  # noqa: S105 - documented dev/test-only default
_REMOVED_ENV = ("RL_DATASET_DIR", "RL_DATASET_PATH")


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Locate the monorepo root (the directory holding ``pyproject.toml`` and ``data/pipeline``).

    Falls back to the current working directory when the package is installed outside the repo
    (e.g. a non-editable container install), in which case every path should come from ``RL_*``.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "data" / "pipeline").is_dir():
            return parent
    return Path.cwd()


def _default(rel: str) -> Path:
    return repo_root() / rel


class PipelineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RL_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    # Parent of every dataset's download directory: a dataset lives in
    # ``<datasets_dir>/<spec.subdir>/<spec.source.filename>`` (compose mounts it at /datasets:ro).
    # A single run can still point at another directory with the task param ``dataset_dir``.
    datasets_dir: Path = Field(default_factory=lambda: _default("data/lake/downloads"))
    lake_dir: Path = Field(default_factory=lambda: _default("data/lake"))
    docs_data_dir: Path = Field(default_factory=lambda: _default("docs/data"))
    # dbt-duckdb project (M3a). Its DuckDB file, target/ and logs/ live under <lake_dir>/dbt.
    dbt_project_dir: Path = Field(default_factory=lambda: _default("data/dbt"))

    s3_endpoint: str = "http://localhost:29000"
    s3_access_key: str = "rlens"
    s3_secret_key: SecretStr = SecretStr(_DEV_SECRET)
    s3_region: str = "us-east-1"
    s3_bucket_data: str = "releaselens-data"
    s3_bucket_mlflow: str = "releaselens-mlflow"

    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = Field(default=4, ge=1, le=64)
    # Upper bound for DuckDB's spill (the full conversion measured ~1.2 GiB at 1GB memory).
    duckdb_max_temp_mib: int = Field(default=3072, ge=64)

    @model_validator(mode="after")
    def _no_dev_secret_in_prod(self) -> PipelineSettings:
        if self.env == "prod" and self.s3_secret_key.get_secret_value() == _DEV_SECRET:
            raise ValueError("RL_S3_SECRET_KEY must be set explicitly when RL_ENV=prod")
        return self

    @model_validator(mode="after")
    def _no_removed_dataset_dir_vars(self) -> PipelineSettings:
        # The single-dataset variables were replaced by RL_DATASETS_DIR (M2b, ADR-0017). Silently
        # ignoring a stale value would look for the dataset in the wrong place.
        stale = sorted(v for v in _REMOVED_ENV if v in os.environ)
        if stale:
            raise ValueError(
                f"{', '.join(stale)} is no longer supported: set RL_DATASETS_DIR to the parent "
                "directory (each dataset lives in its own sub-directory), or pass the task param "
                "dataset_dir for a single run"
            )
        return self

    @property
    def raw_travistorrent_dir(self) -> Path:
        """Every registered release lands here, under ``version=<md5>/`` (md5s differ)."""
        return self.lake_dir / "raw" / "travistorrent"

    def dataset_dir_for(self, subdir: str) -> Path:
        """Download directory of one dataset (``<datasets_dir>/<subdir>``)."""
        return self.datasets_dir / subdir

    @property
    def lake_downloads_dir(self) -> Path:
        """Writable fallback for downloads when ``datasets_dir`` is a read-only mount."""
        return self.lake_dir / "downloads"
