"""Worker configuration from ``RL_*`` environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RL_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "releaselens-worker"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    database_url: str = "postgresql+asyncpg://rlens:rlens@localhost:25432/rlens"
    kafka_bootstrap: str = "localhost:29092"

    health_host: str = "0.0.0.0"  # noqa: S104  # nosec B104 - container health port
    health_port: int = Field(default=8001, ge=1, le=65535)
