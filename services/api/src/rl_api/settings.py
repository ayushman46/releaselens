"""API configuration from ``RL_*`` environment variables (docs/dev/CONVENTIONS.md §2)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RL_", env_file=None, extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "releaselens-api"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    database_url: str = "postgresql+asyncpg://rlens:rlens@localhost:25432/rlens"
    db_pool_size: int = Field(default=5, ge=1, le=50)
    kafka_bootstrap: str = "localhost:29092"
    readiness_timeout_s: float = Field(default=2.0, gt=0, le=10)

    # NoDecode: accept a plain comma-separated env value instead of requiring JSON.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:23000"]
    )

    otel_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
