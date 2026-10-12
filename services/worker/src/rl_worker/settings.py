"""Worker configuration from ``RL_*`` environment variables."""

from __future__ import annotations

import os
import socket
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_worker_id() -> str:
    # Unique per process: job claims and Kafka client ids use it to tell workers apart.
    return f"{socket.gethostname()}-{os.getpid()}"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RL_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "releaselens-worker"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    worker_id: str = Field(default_factory=_default_worker_id, min_length=1, max_length=200)

    database_url: str = "postgresql+asyncpg://rlens:rlens@localhost:25432/rlens"
    db_pool_size: int = Field(default=4, ge=1, le=50)
    # A database that stops answering must surface as a (transient) error, not a hang.
    db_connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    db_command_timeout_s: float = Field(default=15.0, gt=0, le=600)

    kafka_bootstrap: str = "localhost:29092"
    kafka_delivery_timeout_s: float = Field(default=10.0, gt=0, le=120)

    # deployment.events consumer (spec §10)
    deployments_group_id: str = "rl-deployments"
    consumer_poll_timeout_s: float = Field(default=1.0, gt=0, le=10)
    retry_base_s: float = Field(default=0.5, gt=0)
    retry_cap_s: float = Field(default=30.0, gt=0)
    retry_max_attempts: int = Field(default=5, ge=1, le=100)
    # End of the post-deploy window: when the "final" analysis job runs.
    analysis_post_window_end_s: int = Field(default=900, ge=600, le=6 * 3600)

    # Analysis job runner (started only when handlers are registered)
    job_batch_size: int = Field(default=10, ge=1, le=1000)
    job_poll_interval_s: float = Field(default=2.0, gt=0, le=60)
    job_lease_s: int = Field(default=600, ge=30)
    job_timeout_s: int = Field(default=300, ge=1)

    # Outbox relay
    outbox_enabled: bool = True
    outbox_batch_size: int = Field(default=100, ge=1, le=10_000)
    outbox_poll_interval_s: float = Field(default=1.0, gt=0, le=60)

    shutdown_timeout_s: float = Field(default=20.0, gt=0, le=300)

    health_host: str = "0.0.0.0"  # noqa: S104  # nosec B104 - container health port
    health_port: int = Field(default=8001, ge=1, le=65535)

    @model_validator(mode="after")
    def _job_timeout_inside_lease(self) -> Self:
        # JobRunner enforces this too, but it only starts once M8 registers handlers. Checked
        # here, a bad value fails at startup today instead of on the first deploy with M8.
        if self.job_timeout_s >= self.job_lease_s:
            raise ValueError("RL_JOB_TIMEOUT_S must be shorter than RL_JOB_LEASE_S")
        return self
