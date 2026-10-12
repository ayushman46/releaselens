"""Pipeline configuration from ``RL_*`` environment variables (pydantic-settings).

| Variable | Default |
|---|---|
| ``RL_DATASET_DIR`` (alias ``RL_DATASET_PATH``) | ``<repo>/data/lake/downloads/<source name>`` |
| ``RL_LAKE_DIR`` | ``<repo>/data/lake`` |
| ``RL_DOCS_DATA_DIR`` | ``<repo>/docs/data`` |
| ``RL_S3_ENDPOINT`` | ``http://localhost:29000`` |
| ``RL_S3_ACCESS_KEY`` / ``RL_S3_SECRET_KEY`` | ``rlens`` / ``rlens-dev-secret`` (dev/test only) |
| ``RL_S3_REGION`` | ``us-east-1`` |
| ``RL_S3_BUCKET_DATA`` / ``RL_S3_BUCKET_MLFLOW`` | ``releaselens-data`` / ``releaselens-mlflow`` |
| ``RL_DUCKDB_MEMORY_LIMIT`` / ``RL_DUCKDB_THREADS`` | ``1GB`` / ``4`` |
| ``RL_DUCKDB_MAX_TEMP_MIB`` | ``3072`` (spill cap; further limited by free disk) |
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "rlens-dev-secret"  # noqa: S105 - documented dev/test-only default


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

    dataset_dir: Path = Field(
        default_factory=lambda: _default("data/lake/downloads/travistorrent-figshare-19314170"),
        validation_alias=AliasChoices("RL_DATASET_DIR", "RL_DATASET_PATH"),
    )
    lake_dir: Path = Field(default_factory=lambda: _default("data/lake"))
    docs_data_dir: Path = Field(default_factory=lambda: _default("docs/data"))

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

    @property
    def raw_travistorrent_dir(self) -> Path:
        return self.lake_dir / "raw" / "travistorrent"
