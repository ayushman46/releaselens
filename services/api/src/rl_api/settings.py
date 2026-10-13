"""API configuration from ``RL_*`` environment variables (docs/dev/CONVENTIONS.md §2).

Secrets never have defaults outside ``RL_ENV=dev|test``: in ``prod`` a missing
``RL_SECRET_KEY`` stops the process at startup. Optional integrations (GitHub OAuth, Stripe)
have no defaults anywhere; their endpoints answer 503 until they are configured.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from rl_db.secretbox import DEV_MASTER_KEY

# Used only when RL_ENV is dev or test and RL_SECRET_KEY is unset (shared with the worker).
DEV_SECRET_KEY = DEV_MASTER_KEY

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class RatePolicy:
    """A token bucket: ``burst`` requests at once, refilled at ``per_minute``."""

    name: str
    burst: int
    per_minute: float

    @property
    def refill_per_s(self) -> float:
        return self.per_minute / 60.0


def _csv(value: object) -> object:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


class Settings(BaseSettings):
    # env_ignore_empty: compose passes unset optional secrets as "" (meaning "not set").
    model_config = SettingsConfigDict(
        env_prefix="RL_", env_file=None, extra="ignore", env_ignore_empty=True
    )

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

    # ------------------------------------------------------------------ M10: URLs & secrets
    # Where the web app lives (links in e-mails, redirects after OAuth / Stripe Checkout).
    web_base_url: str = "http://localhost:23000"
    # Public URL of this API (default OAuth redirect URI).
    api_base_url: str = "http://localhost:28000"
    # Master secret: encryption of stored integration secrets and CSRF token derivation.
    secret_key: SecretStr | None = None
    # Old master secrets still accepted for decryption after a rotation (comma-separated).
    secret_key_previous: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)

    # ------------------------------------------------------------------ sessions & cookies
    access_token_ttl_s: int = Field(default=15 * 60, ge=60, le=3600)
    refresh_token_ttl_s: int = Field(default=14 * 86_400, ge=3600, le=90 * 86_400)
    session_max_age_s: int = Field(default=30 * 86_400, ge=3600, le=365 * 86_400)
    cookie_domain: str | None = None

    # ------------------------------------------------------------------ passwords (Argon2id)
    # RFC 9106 §4 "second recommended option": t=3, m=64 MiB, p=4.
    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_kib: int = Field(default=64 * 1024, ge=8, le=1024 * 1024)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)
    # Concurrent hashes per process: each one holds argon2_memory_kib of RAM.
    password_hash_concurrency: int = Field(default=2, ge=1, le=32)

    # ------------------------------------------------------------------ e-mail (SMTP)
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_timeout_s: float = Field(default=10.0, gt=0, le=60)
    mail_from: str = "ReleaseLens <no-reply@releaselens.local>"

    # ------------------------------------------------------------------ GitHub OAuth (B5)
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None
    github_oauth_redirect_uri: str | None = None

    # ------------------------------------------------------------------ Stripe (B2), test mode only
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_price_team: str | None = None

    # ------------------------------------------------------------------ risk model (M5)
    # Loaded ONCE at startup (and retried in the background until it loads). A ``models:/``
    # URI is resolved in the MLflow registry and its ``risk_bundle`` cached under
    # RL_MODEL_CACHE_DIR with SHA-256 checksums; a local directory is loaded as is.
    # Empty URI: no model, and POST /api/v1/risk/assessments answers 503 no_model_loaded.
    model_uri: str | None = "models:/release_risk_ci@champion"
    # Repositories with no finished builds are scored by a model trained without history.
    cold_model_uri: str | None = "models:/release_risk_ci_cold@champion"
    mlflow_tracking_uri: str | None = None
    model_cache_dir: str = "~/.cache/releaselens/models"
    model_retry_interval_s: float = Field(default=60.0, ge=0, le=86_400)
    # How long startup waits for the first load attempt before serving (503 until loaded).
    model_startup_wait_s: float = Field(default=30.0, ge=0, le=600)
    # Finished runs loaded per assessment (the history features need the last 20; totals are
    # counted exactly in SQL).
    risk_history_limit: int = Field(default=200, ge=20, le=10_000)
    # Same (repository, commit, event, PR) within this window returns the stored assessment.
    risk_idempotency_window_s: int = Field(default=600, ge=0, le=86_400)

    # ------------------------------------------------------------------ GitHub App (B1)
    github_app_id: str | None = None
    github_app_private_key_file: str | None = None
    github_webhook_secret: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_check_run_name: str = "ReleaseLens risk"
    github_webhook_max_body_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024
    )

    # ------------------------------------------------------------------ request hygiene
    # Proxies whose X-Forwarded-For is believed (CIDRs, comma-separated). Empty: none.
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024, le=64 * 1024 * 1024)

    # ------------------------------------------------------------------ rate limits
    rate_limit_enabled: bool = True
    # Per client IP, shared by login, signup, verification, password reset and OAuth.
    rate_limit_auth_burst: int = Field(default=20, ge=1)
    rate_limit_auth_per_minute: float = Field(default=10.0, gt=0)
    # Per (account, IP) on login: slows password guessing against one account without
    # letting a third party lock the owner out from their own address.
    rate_limit_login_account_burst: int = Field(default=5, ge=1)
    rate_limit_login_account_per_minute: float = Field(default=1.0, gt=0)
    # Per API key on ingest endpoints (deployments, later /v1/traces).
    rate_limit_ingest_burst: int = Field(default=200, ge=1)
    rate_limit_ingest_per_minute: float = Field(default=3000.0, gt=0)
    # Per API key on POST /api/v1/risk/assessments and /api/v1/ci-runs.
    rate_limit_risk_burst: int = Field(default=60, ge=1)
    rate_limit_risk_per_minute: float = Field(default=600.0, gt=0)

    @field_validator("cors_origins", "trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        return _csv(value)

    @field_validator("secret_key_previous", mode="before")
    @classmethod
    def _split_secret_csv(cls, value: object) -> object:
        return _csv(value)

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_cidrs(cls, value: list[str]) -> list[str]:
        for cidr in value:
            ipaddress.ip_network(cidr, strict=False)  # raises ValueError on garbage
        return value

    @model_validator(mode="after")
    def _check_security(self) -> Self:
        if "*" in self.cors_origins:
            # Credentialed CORS must name its origins (and browsers refuse "*" with them).
            raise ValueError("RL_CORS_ORIGINS must list origins explicitly, not '*'")
        if self.env == "prod":
            key = self.secret_key.get_secret_value() if self.secret_key else ""
            if len(key) < 32:
                raise ValueError("RL_SECRET_KEY (>= 32 characters) is required when RL_ENV=prod")
            if key == DEV_SECRET_KEY:
                raise ValueError("RL_SECRET_KEY must not be the public development key in prod")
        elif self.secret_key is not None and len(self.secret_key.get_secret_value()) < 32:
            raise ValueError("RL_SECRET_KEY must be at least 32 characters")
        if self.stripe_secret_key is not None:
            sk = self.stripe_secret_key.get_secret_value()
            # Test mode only (ADR-0014 pin; BLOCKERS.md B2): a live key is refused outright.
            if not sk.startswith(("sk_test_", "rk_test_")):
                raise ValueError("RL_STRIPE_SECRET_KEY must be a Stripe test-mode key")
        if self.github_app_id is not None and not self.github_app_id.isdigit():
            raise ValueError("RL_GITHUB_APP_ID must be the numeric App id")
        if self.env == "prod" and not self.github_api_url.startswith("https://"):
            raise ValueError("RL_GITHUB_API_URL must use https in prod")
        if self.session_max_age_s < self.access_token_ttl_s:
            raise ValueError("RL_SESSION_MAX_AGE_S must be >= RL_ACCESS_TOKEN_TTL_S")
        return self

    # ------------------------------------------------------------------ derived values
    def master_secret(self) -> str:
        if self.secret_key is not None:
            return self.secret_key.get_secret_value()
        return DEV_SECRET_KEY  # only reachable in dev/test (validated above)

    def previous_secrets(self) -> list[str]:
        return [s.get_secret_value() for s in self.secret_key_previous]

    def trusted_proxy_networks(self) -> list[IPNetwork]:
        return [ipaddress.ip_network(c, strict=False) for c in self.trusted_proxies]

    @property
    def github_oauth_configured(self) -> bool:
        return bool(self.github_oauth_client_id and self.github_oauth_client_secret)

    def github_redirect_uri(self) -> str:
        return self.github_oauth_redirect_uri or (
            f"{self.api_base_url.rstrip('/')}/api/v1/auth/github/callback"
        )

    @property
    def github_app_configured(self) -> bool:
        """Check Runs need the App id and its private key."""
        return bool(self.github_app_id and self.github_app_private_key_file)

    @property
    def stripe_configured(self) -> bool:
        return self.stripe_secret_key is not None

    @property
    def allow_any_slack_host(self) -> bool:
        """Dev and test may post alerts to a local webhook mock instead of Slack."""
        return self.env in ("dev", "test")

    def web_url(self, path: str) -> str:
        return f"{self.web_base_url.rstrip('/')}/{path.lstrip('/')}"

    def rate_policy(self, name: Literal["auth", "login_account", "ingest", "risk"]) -> RatePolicy:
        if name == "auth":
            return RatePolicy("auth", self.rate_limit_auth_burst, self.rate_limit_auth_per_minute)
        if name == "login_account":
            return RatePolicy(
                "login_account",
                self.rate_limit_login_account_burst,
                self.rate_limit_login_account_per_minute,
            )
        if name == "risk":
            return RatePolicy("risk", self.rate_limit_risk_burst, self.rate_limit_risk_per_minute)
        return RatePolicy("ingest", self.rate_limit_ingest_burst, self.rate_limit_ingest_per_minute)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
