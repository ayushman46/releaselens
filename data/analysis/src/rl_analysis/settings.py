"""Analysis configuration from ``RL_*`` environment variables (pydantic-settings).

The same variables as ``rl_pipeline.settings`` (docs/dev/CONVENTIONS.md §2), read independently:
``rl_analysis`` must not import ``rl_pipeline``, because the pipeline's
``export_analytics_to_postgres`` task will import ``rl_analysis`` (no dependency cycle).

| Variable | Default |
|---|---|
| ``RL_S3_ENDPOINT`` | ``http://localhost:29000`` |
| ``RL_S3_ACCESS_KEY`` / ``RL_S3_SECRET_KEY`` | ``rlens`` / ``rlens-dev-secret`` (dev/test only) |
| ``RL_S3_REGION`` | ``us-east-1`` |
| ``RL_S3_BUCKET_DATA`` | ``releaselens-data`` |
| ``RL_DUCKDB_MEMORY_LIMIT`` / ``RL_DUCKDB_THREADS`` | ``1GB`` / ``2`` |
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "rlens-dev-secret"  # noqa: S105 - documented dev/test-only default


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """The monorepo root (holds ``pyproject.toml`` and ``data/analysis``), else the CWD."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "data" / "analysis").is_dir():
            return parent
    return Path.cwd()


class AnalysisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RL_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    s3_endpoint: str = "http://localhost:29000"
    s3_access_key: str = "rlens"
    s3_secret_key: SecretStr = SecretStr(_DEV_SECRET)
    s3_region: str = "us-east-1"
    s3_bucket_data: str = "releaselens-data"

    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = Field(default=2, ge=1, le=64)

    @model_validator(mode="after")
    def _no_dev_secret_in_prod(self) -> AnalysisSettings:
        if self.env == "prod" and self.s3_secret_key.get_secret_value() == _DEV_SECRET:
            raise ValueError("RL_S3_SECRET_KEY must be set explicitly when RL_ENV=prod")
        return self

    @property
    def default_input_root(self) -> str:
        """Where dbt writes marts and features (``s3://<RL_S3_BUCKET_DATA>``)."""
        return f"s3://{self.s3_bucket_data}"
