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
    # Upper bound on waiting for the broker's ack before answering 503 (see rl_api.kafka).
    kafka_delivery_timeout_s: float = Field(default=5.0, gt=0, le=60)
    # OTLP ingest (POST /v1/traces). The compressed cap bounds what is read from the socket;
    # the decompressed cap bounds what a gzip body may inflate to (gzip bombs).
    otlp_max_body_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    otlp_max_decompressed_bytes: int = Field(
        default=16 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024
    )
    # Accepted span start times: at most this far ahead of the server clock, and no older than
    # the span retention (older spans would land in partitions retention is about to drop).
    otlp_max_future_skew_s: float = Field(default=86_400.0, gt=0, le=7 * 86_400)
    otlp_max_span_age_s: float = Field(default=7 * 86_400.0, gt=0, le=90 * 86_400)
    # API keys: write last_used_at at most this often per key (0 = on every request).
    api_key_touch_interval_s: float = Field(default=60.0, ge=0, le=86_400)

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
