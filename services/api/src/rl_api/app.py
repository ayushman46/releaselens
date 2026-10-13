"""Application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rl_api import __version__
from rl_api.billing import BillingGateway, StripeGateway
from rl_api.errors import install_error_handlers
from rl_api.github_app import GitHubAppClient, load_private_key
from rl_api.health import kafka_check, postgres_check
from rl_api.health import router as health_router
from rl_api.kafka import EventProducer
from rl_api.mail import Mailer, SmtpMailer
from rl_api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from rl_api.passwords import PasswordHasher
from rl_api.risk.modelstore import ModelStore
from rl_api.routers.alerts import router as alerts_router
from rl_api.routers.analysis import router as analysis_router
from rl_api.routers.apikeys import router as apikeys_router
from rl_api.routers.audit import router as audit_router
from rl_api.routers.auth import me_router
from rl_api.routers.auth import router as auth_router
from rl_api.routers.billing import router as billing_router
from rl_api.routers.ci_runs import router as ci_runs_router
from rl_api.routers.deployments import router as deployments_router
from rl_api.routers.github import router as github_router
from rl_api.routers.orgs import router as orgs_router
from rl_api.routers.otlp import router as otlp_router
from rl_api.routers.overview import router as overview_router
from rl_api.routers.projects import router as projects_router
from rl_api.routers.risk import router as risk_router
from rl_api.settings import Settings, get_settings
from rl_api.telemetry import setup_tracing, shutdown_tracing
from rl_core.obs import configure_logging
from rl_db import create_engine, create_session_factory
from rl_db.secretbox import SecretBox, derive_key

log = logging.getLogger("rl_api")

# Outgoing calls (GitHub OAuth) must neither hang a request nor follow redirects to
# wherever a response points.
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _default_mailer(settings: Settings) -> Mailer:
    return SmtpMailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.mail_from,
        username=settings.smtp_username,
        password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
        starttls=settings.smtp_starttls,
        timeout_s=settings.smtp_timeout_s,
    )


def create_app(
    settings: Settings | None = None,
    *,
    mailer: Mailer | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    billing_gateway: BillingGateway | None = None,
    model_store: ModelStore | None = None,
) -> FastAPI:
    """Build the app. The keyword arguments replace external services in tests: a
    recording mailer, an ``httpx.MockTransport`` for GitHub, a fake Stripe gateway, a
    model store pointing at a test bundle."""
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url, pool_size=settings.db_pool_size)
        app.state.db_engine = engine
        app.state.db_sessions = create_session_factory(engine)
        producer = EventProducer(
            settings.kafka_bootstrap,
            client_id=settings.service_name,
            delivery_timeout_s=settings.kafka_delivery_timeout_s,
        )
        producer.start()
        app.state.event_producer = producer
        app.state.http_client = httpx.AsyncClient(
            transport=http_transport, timeout=HTTP_TIMEOUT, follow_redirects=False
        )
        app.state.github_app = (
            GitHubAppClient(
                app_id=str(settings.github_app_id),
                private_key=github_key,
                http=app.state.http_client,
                api_url=settings.github_api_url,
            )
            if github_key is not None
            else None
        )
        await app.state.model_store.start(wait_s=settings.model_startup_wait_s)
        app.state.readiness_checks = {
            "postgres": postgres_check(app.state),
            "kafka": kafka_check(settings.kafka_bootstrap, settings.readiness_timeout_s, producer),
        }
        log.info("api started", extra={"version": __version__, "env": settings.env})
        try:
            yield
        finally:
            # Flush acknowledged-pending events before the process exits; requests still
            # waiting on them get their answer during the flush.
            await app.state.model_store.stop()
            await asyncio.to_thread(producer.close, settings.kafka_delivery_timeout_s)
            await app.state.http_client.aclose()
            await engine.dispose()
            shutdown_tracing()
            log.info("api stopped")

    app = FastAPI(
        title="ReleaseLens API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.trusted_proxies = settings.trusted_proxy_networks()
    master = settings.master_secret()
    app.state.secret_box = SecretBox(master, settings.previous_secrets())
    app.state.csrf_key = derive_key(master, "releaselens/csrf/v1")
    app.state.password_hasher = PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_kib=settings.argon2_memory_kib,
        parallelism=settings.argon2_parallelism,
        concurrency=settings.password_hash_concurrency,
    )
    app.state.mailer = mailer or _default_mailer(settings)
    app.state.model_store = model_store or ModelStore(
        uris={"main": settings.model_uri, "cold": settings.cold_model_uri},
        tracking_uri=settings.mlflow_tracking_uri,
        cache_dir=Path(settings.model_cache_dir),
        retry_interval_s=settings.model_retry_interval_s,
    )
    # Read once at startup: a missing or unreadable key file stops the process (fail fast)
    # rather than failing every Check Run later.
    github_key = (
        load_private_key(settings.github_app_private_key_file)
        if settings.github_app_configured and settings.github_app_private_key_file
        else None
    )
    app.state.github_app = None
    if billing_gateway is not None:
        app.state.billing_gateway = billing_gateway
    elif settings.stripe_secret_key is not None:
        app.state.billing_gateway = StripeGateway(settings.stripe_secret_key.get_secret_value())
    else:
        app.state.billing_gateway = None

    # Middleware order (outermost last): request id → security headers → CORS → body limit.
    # OTLP ingest has its own, larger compressed-body cap (and a decompression cap) — the
    # global 1 MiB limit would reject ordinary collector batches with 413.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_body_bytes,
        overrides=[
            ("/v1/traces", settings.otlp_max_body_bytes),
            ("/api/v1/integrations/github/webhook", settings.github_webhook_max_body_bytes),
        ],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token", "X-RL-Org"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.env != "dev")
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(deployments_router)
    app.include_router(analysis_router)
    app.include_router(overview_router)
    app.include_router(otlp_router)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(orgs_router)
    app.include_router(projects_router)
    app.include_router(apikeys_router)
    app.include_router(audit_router)
    app.include_router(alerts_router)
    app.include_router(billing_router)
    app.include_router(risk_router)
    app.include_router(ci_runs_router)
    app.include_router(github_router)

    if settings.otel_enabled:
        setup_tracing(app, settings.service_name, settings.env)
    return app
