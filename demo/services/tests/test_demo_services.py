"""The demo topology must produce real, correctly-parented spans and honour its regression knobs."""

from __future__ import annotations

import asyncio
import contextvars
import statistics
from contextlib import AsyncExitStack

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from rl_demo.app import create_app
from rl_demo.behavior import Behavior, OwnWork


def _behavior(service: str, **kw: float | str) -> Behavior:
    base: dict[str, float | str] = {
        "service": service,
        "version": "v1",
        "environment": "test",
        "own_latency_ms": 1.0,
        "sigma": 0.0,
        "extra_latency_ms": 0.0,
        "error_rate": 0.0,
        "seed": 0,
    }
    base.update(kw)
    return Behavior(**base)  # type: ignore[arg-type]


class HostRouter(httpx.AsyncBaseTransport):
    """Route requests to in-process ASGI apps by hostname (http://payment:8000 → payment app).

    Each downstream call runs in a *fresh* ``contextvars.Context`` so the callee cannot see the
    caller's active span — exactly like a real process boundary, where trace context travels
    only in the W3C ``traceparent`` header. Each app gets its own router instance (sharing the
    route table) because HTTPX instrumentation wraps the transport it is given.
    """

    def __init__(self, routes: dict[str, httpx.ASGITransport]) -> None:
        self.routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self.routes[request.url.host]
        task = asyncio.create_task(
            target.handle_async_request(request), context=contextvars.Context()
        )
        return await task


async def _run_chain(
    overrides: dict[str, dict[str, float | str]], requests: int
) -> tuple[list[int], InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    routes: dict[str, httpx.ASGITransport] = {}
    apps = {
        name: create_app(
            _behavior(name, **overrides.get(name, {})),
            span_exporter=exporter,
            transport=HostRouter(routes),
        )
        for name in ("gateway", "orders", "inventory", "payment", "fraud")
    }
    for name, app in apps.items():
        routes[name] = httpx.ASGITransport(app=app)
    statuses: list[int] = []
    async with AsyncExitStack() as stack:
        for app in apps.values():
            await stack.enter_async_context(app.router.lifespan_context(app))
        async with httpx.AsyncClient(
            transport=HostRouter(routes), base_url="http://gateway:8000"
        ) as client:
            for _ in range(requests):
                statuses.append((await client.get("/checkout")).status_code)
    return statuses, exporter


def test_own_work_is_seeded_and_honours_regression_knobs() -> None:
    a = OwnWork(_behavior("payment", own_latency_ms=60.0, sigma=0.25, seed=7))
    b = OwnWork(_behavior("payment", own_latency_ms=60.0, sigma=0.25, seed=7))
    assert [a.draw() for _ in range(50)] == [b.draw() for _ in range(50)]

    v1 = OwnWork(_behavior("payment", own_latency_ms=60.0, sigma=0.25, seed=1))
    v2 = OwnWork(
        _behavior("payment", own_latency_ms=60.0, sigma=0.25, seed=1, extra_latency_ms=250.0)
    )
    d1 = [v1.draw()[0] for _ in range(2000)]
    d2 = [v2.draw()[0] for _ in range(2000)]
    assert statistics.median(d2) - statistics.median(d1) == pytest.approx(250.0, abs=1e-6)

    err = OwnWork(_behavior("payment", error_rate=0.035, seed=3))
    rate = sum(err.draw()[1] for _ in range(20_000)) / 20_000
    assert rate == pytest.approx(0.035, abs=0.005)


def test_behavior_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RL_DEMO_SERVICE", "nope")
    with pytest.raises(ValueError, match="RL_DEMO_SERVICE"):
        Behavior.from_env()
    monkeypatch.setenv("RL_DEMO_SERVICE", "fraud")
    monkeypatch.setenv("RL_DEMO_ERROR_RATE", "1.5")
    with pytest.raises(ValueError, match="ERROR_RATE"):
        Behavior.from_env()


async def test_full_chain_produces_linked_cross_service_traces() -> None:
    statuses, exporter = await _run_chain({}, requests=3)
    assert statuses == [200, 200, 200]
    spans = exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    traces = {s.context.trace_id for s in spans}
    assert len(traces) == 3  # one trace per k6-style request

    services = {s.resource.attributes["service.name"] for s in spans}
    assert services == {"gateway", "orders", "inventory", "payment", "fraud"}

    # Every SERVER span except the root's has a CLIENT parent in the calling service.
    edges: set[tuple[str, str]] = set()
    for s in spans:
        if s.kind is SpanKind.SERVER and s.parent is not None:
            parent = by_id[s.parent.span_id]
            assert parent.kind is SpanKind.CLIENT
            edges.add(
                (
                    str(parent.resource.attributes["service.name"]),
                    str(s.resource.attributes["service.name"]),
                )
            )
    assert edges == {
        ("gateway", "orders"),
        ("orders", "inventory"),
        ("orders", "payment"),
        ("payment", "fraud"),
    }
    own = [s for s in spans if s.name.endswith(".own_work")]
    assert {s.resource.attributes["service.name"] for s in own} == services


async def test_payment_failure_propagates_and_marks_spans() -> None:
    statuses, exporter = await _run_chain({"payment": {"error_rate": 1.0}}, requests=2)
    assert statuses == [502, 502]  # gateway sees orders' 502, which saw payment's 500
    spans = exporter.get_finished_spans()
    payment_server = [
        s
        for s in spans
        if s.kind is SpanKind.SERVER and s.resource.attributes["service.name"] == "payment"
    ]
    assert payment_server
    assert all(s.status.status_code is StatusCode.ERROR for s in payment_server)
    fraud = [s for s in spans if s.resource.attributes["service.name"] == "fraud"]
    assert fraud == []  # payment fails in its own work before calling fraud


async def test_version_attribute_follows_deployment() -> None:
    _, exporter = await _run_chain({"payment": {"version": "v2"}}, requests=1)
    versions = {
        s.resource.attributes["service.version"]
        for s in exporter.get_finished_spans()
        if s.resource.attributes["service.name"] == "payment"
    }
    assert versions == {"v2"}
