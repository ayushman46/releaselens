"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rl_api import __version__
from rl_api.errors import install_error_handlers
from rl_api.health import kafka_check, postgres_check
from rl_api.health import router as health_router
from rl_api.middleware import RequestContextMiddleware
from rl_api.settings import Settings, get_settings
from rl_api.telemetry import setup_tracing, shutdown_tracing
from rl_core.obs import configure_logging
from rl_db import create_engine, create_session_factory

log = logging.getLogger("rl_api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url, pool_size=settings.db_pool_size)
        app.state.db_engine = engine
        app.state.db_sessions = create_session_factory(engine)
        app.state.readiness_checks = {
            "postgres": postgres_check(app.state),
            "kafka": kafka_check(settings.kafka_bootstrap, settings.readiness_timeout_s),
        }
        log.info("api started", extra={"version": __version__, "env": settings.env})
        try:
            yield
        finally:
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)

    if settings.otel_enabled:
        setup_tracing(app, settings.service_name, settings.env)
    return app
