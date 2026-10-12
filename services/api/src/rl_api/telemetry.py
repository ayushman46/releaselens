"""ReleaseLens' own OpenTelemetry setup (self-observability, not customer telemetry).

Spans are always created (so logs carry trace/span ids). They are exported over OTLP/HTTP
only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from rl_api import __version__

_provider: TracerProvider | None = None


def setup_tracing(app: FastAPI, service_name: str, env: str) -> None:
    global _provider
    if _provider is None:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": __version__,
                "deployment.environment": env,
            }
        )
        _provider = TracerProvider(resource=resource)
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(_provider)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_provider, excluded_urls="healthz,readyz"
    )


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.shutdown()
