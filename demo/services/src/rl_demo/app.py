"""One FastAPI app factory for all five demo services (same code, different routes/config).

Spans produced per request:
* SERVER span per inbound request (FastAPI instrumentation)
* INTERNAL span ``<service>.own_work`` for the service's own processing time
* CLIENT span per downstream call (HTTPX instrumentation, W3C trace-context propagated)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode

from rl_demo.behavior import Behavior, InjectedError, OwnWork

DOWNSTREAM_DEFAULTS = {
    "gateway": {"orders": "http://orders:8000"},
    "orders": {"inventory": "http://inventory:8000", "payment": "http://payment:8000"},
    "payment": {"fraud": "http://fraud:8000"},
    "inventory": {},
    "fraud": {},
}


def _downstream_url(service: str, target: str) -> str:
    return os.environ.get(f"RL_DEMO_{target.upper()}_URL", DOWNSTREAM_DEFAULTS[service][target])


def _setup_tracing(b: Behavior, span_exporter: SpanExporter | None) -> TracerProvider:
    resource = Resource.create(
        {
            "service.name": b.service,
            "service.version": b.version,
            "deployment.environment": b.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    if span_exporter is not None:  # tests: synchronous, in-memory
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def create_app(
    behavior: Behavior | None = None,
    *,
    span_exporter: SpanExporter | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build one demo service. ``span_exporter``/``transport`` exist for in-process tests."""
    b = behavior or Behavior.from_env()
    provider = _setup_tracing(b, span_exporter)
    tracer = provider.get_tracer("rl_demo")
    work = OwnWork(b)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0), transport=transport)
        HTTPXClientInstrumentor.instrument_client(client, tracer_provider=provider)
        app.state.client = client
        try:
            yield
        finally:
            await client.aclose()
            provider.shutdown()

    app = FastAPI(title=f"demo-{b.service}", version=b.version, lifespan=lifespan)

    async def own_work() -> None:
        with tracer.start_as_current_span(f"{b.service}.own_work") as span:
            try:
                await work.perform()
            except InjectedError as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def call(target: str, method: str, path: str) -> None:
        client: httpx.AsyncClient = app.state.client
        resp = await client.request(method, _downstream_url(b.service, target) + path)
        if resp.status_code >= 500:
            # Propagation: an upstream sees the downstream failure and fails its own request.
            raise HTTPException(status_code=502, detail=f"{target} returned {resp.status_code}")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": b.service, "version": b.version}

    if b.service == "gateway":

        @app.get("/checkout")
        async def checkout() -> dict[str, str]:
            await own_work()
            await call("orders", "POST", "/orders")
            return {"status": "ok"}

    elif b.service == "orders":

        @app.post("/orders")
        async def create_order() -> dict[str, str]:
            await own_work()
            # inventory and payment run concurrently → overlapping child intervals (self-time
            # must count their union once).
            await asyncio.gather(
                call("inventory", "GET", "/reserve"), call("payment", "POST", "/charge")
            )
            return {"status": "created"}

    elif b.service == "inventory":

        @app.get("/reserve")
        async def reserve() -> dict[str, str]:
            await own_work()
            return {"status": "reserved"}

    elif b.service == "payment":

        @app.post("/charge")
        async def charge() -> dict[str, str]:
            await own_work()
            await call("fraud", "POST", "/score")
            return {"status": "charged"}

    elif b.service == "fraud":

        @app.post("/score")
        async def score() -> dict[str, str]:
            await own_work()
            return {"status": "scored"}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="healthz")
    return app
