"""Deterministic synthetic trace generator for regression-engine tests (tests only).

Topology (the demo's, see ``demo/services/src/rl_demo/app.py``)::

    k6 (not instrumented) → gateway → orders → { inventory , payment → fraud }

Each service handles a request exactly like the demo app does:

* a SERVER span for the inbound request (root for gateway);
* an INTERNAL ``<service>.own_work`` span — the service's own processing, lognormal with median
  ``own_latency_ms`` and spread ``sigma`` plus ``extra_latency_ms``; it fails with probability
  ``error_rate`` (status ERROR), and the SERVER span then answers HTTP 500 without calling
  anything downstream;
* a CLIENT span per downstream call (network ``network_ms`` each way). ``orders`` calls
  ``inventory`` and ``payment`` **concurrently** (overlapping child intervals). A callee answering
  5xx makes the caller answer 502 (propagation); like ``asyncio.gather``, the caller returns as soon
  as the first failure arrives while the other call keeps running, so that CLIENT span can
  outlive its parent SERVER span.

Behaviour switches at the deploy instant ``T`` per service span: a service span starting at or
after ``T`` uses the ``after`` behaviour (and the deployed service reports the new version).

Everything is driven by one ``random.Random(seed)``: the same scenario, ``T`` and seed always
produce the same spans.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from rl_core.regression.evidence import DeploymentRef
from rl_core.telemetry.model import NS_PER_MS, NS_PER_S, Span, SpanKind, StatusCode, datetime_to_ns

T_DEPLOY = datetime(2026, 10, 13, 14, 2, tzinfo=UTC)

SERVICES = ("gateway", "orders", "inventory", "payment", "fraud")

# Same medians as the demo services (rl_demo.behavior.DEFAULT_OWN_LATENCY_MS).
DEFAULT_OWN_LATENCY_MS: Mapping[str, float] = {
    "gateway": 4.0,
    "orders": 12.0,
    "inventory": 18.0,
    "payment": 60.0,
    "fraud": 35.0,
}

CALLS: Mapping[str, tuple[str, ...]] = {
    "gateway": ("orders",),
    "orders": ("inventory", "payment"),  # concurrent
    "inventory": (),
    "payment": ("fraud",),
    "fraud": (),
}

ROUTES: Mapping[str, str] = {
    "gateway": "/checkout",
    "orders": "/orders",
    "inventory": "/reserve",
    "payment": "/charge",
    "fraud": "/score",
}

BASELINE_ERROR_RATE = 0.001  # every service fails 0.1% of its own work, before and after


@dataclass(frozen=True, slots=True)
class Behaviour:
    own_latency_ms: float
    sigma: float = 0.25
    extra_latency_ms: float = 0.0
    error_rate: float = BASELINE_ERROR_RATE


#: A call to a service that is down (crashed / not listening) is refused after this long.
CONNECT_FAIL_MS = 2.0


def default_behaviour() -> dict[str, Behaviour]:
    return {s: Behaviour(own_latency_ms=ms) for s, ms in DEFAULT_OWN_LATENCY_MS.items()}


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    deployed_service: str
    previous_version: str
    version: str
    before: Mapping[str, Behaviour] = field(default_factory=default_behaviour)
    after: Mapping[str, Behaviour] = field(default_factory=default_behaviour)
    rate_per_minute: float = 60.0
    start_minutes: float = -31.0  # relative to T: a little before the baseline window
    end_minutes: float = 15.5  # a little after the post window
    environment: str = "demo"
    tenant_id: str = "tenant-a"
    network_ms: float = 1.0
    # Services that are down from T on: callers' CLIENT spans fail after ``CONNECT_FAIL_MS`` with
    # status ERROR and no HTTP status, and no span of the down service is recorded.
    down_after_deploy: tuple[str, ...] = ()

    def deployment(self, deployed_at: datetime = T_DEPLOY) -> DeploymentRef:
        return DeploymentRef(
            service=self.deployed_service,
            environment=self.environment,
            version=self.version,
            previous_version=self.previous_version,
            deployed_at=deployed_at,
        )


# ----------------------------------------------------------------------------- scenarios
def _with(base: Mapping[str, Behaviour], service: str, **changes: float) -> dict[str, Behaviour]:
    out = dict(base)
    out[service] = replace(out[service], **changes)
    return out


def payment_regression(rate_per_minute: float = 60.0) -> Scenario:
    """Scenario 1: payment v2.17 → v2.18 adds +250 ms to its own path and +3.5% own errors."""
    before = default_behaviour()
    after = _with(before, "payment", extra_latency_ms=250.0, error_rate=BASELINE_ERROR_RATE + 0.035)
    return Scenario(
        name="payment_regression",
        deployed_service="payment",
        previous_version="v2.17",
        version="v2.18",
        before=before,
        after=after,
        rate_per_minute=rate_per_minute,
    )


def control(rate_per_minute: float = 60.0) -> Scenario:
    """Control: payment v1 → v1, nothing changes."""
    return Scenario(
        name="control",
        deployed_service="payment",
        previous_version="v1",
        version="v1",
        rate_per_minute=rate_per_minute,
    )


def fraud_regression(rate_per_minute: float = 60.0) -> Scenario:
    """Scenario 2: fraud v1.4 → v1.5 adds +250 ms to fraud's own path; payment unchanged."""
    before = default_behaviour()
    return Scenario(
        name="fraud_regression",
        deployed_service="fraud",
        previous_version="v1.4",
        version="v1.5",
        before=before,
        after=_with(before, "fraud", extra_latency_ms=250.0),
        rate_per_minute=rate_per_minute,
    )


def fraud_regression_payment_deployed(rate_per_minute: float = 60.0) -> Scenario:
    """Scenario 2b: payment is deployed (no behaviour change) while fraud's own path slows."""
    before = default_behaviour()
    return Scenario(
        name="fraud_regression_payment_deployed",
        deployed_service="payment",
        previous_version="v2.17",
        version="v2.18",
        before=before,
        after=_with(before, "fraud", extra_latency_ms=250.0),
        rate_per_minute=rate_per_minute,
    )


def payment_crash(rate_per_minute: float = 60.0) -> Scenario:
    """payment v2.18 crashes at deploy: from T on, every call to payment is refused."""
    before = default_behaviour()
    return Scenario(
        name="payment_crash",
        deployed_service="payment",
        previous_version="v2.17",
        version="v2.18",
        before=before,
        after=before,
        rate_per_minute=rate_per_minute,
        down_after_deploy=("payment",),
    )


def payment_errors_only(rate_per_minute: float = 60.0) -> Scenario:
    """payment v2.18 fails +3.5% of its own work; its latency is unchanged."""
    before = default_behaviour()
    return Scenario(
        name="payment_errors_only",
        deployed_service="payment",
        previous_version="v2.17",
        version="v2.18",
        before=before,
        after=_with(before, "payment", error_rate=BASELINE_ERROR_RATE + 0.035),
        rate_per_minute=rate_per_minute,
    )


def low_traffic() -> Scenario:
    """Scenario 1's change at 5 requests/minute: fewer than 200 requests in each window."""
    return replace(payment_regression(rate_per_minute=5.0), name="low_traffic")


# ----------------------------------------------------------------------------- generator
class _Builder:
    def __init__(self, scenario: Scenario, deployed_at_ns: int, rng: random.Random) -> None:
        self.s = scenario
        self.t_ns = deployed_at_ns
        self.rng = rng
        self.spans: list[Span] = []
        self._next_span = 1
        self._next_trace = 1

    def _span_id(self) -> str:
        value = self._next_span
        self._next_span += 1
        return f"{value:016x}"

    def trace_id(self) -> str:
        value = self._next_trace
        self._next_trace += 1
        return f"{value:032x}"

    def _ms(self, value: float) -> int:
        return round(value * NS_PER_MS)

    def _version(self, service: str, start_ns: int) -> str:
        if service != self.s.deployed_service:
            return "v1"
        return self.s.version if start_ns >= self.t_ns else self.s.previous_version

    def _emit(
        self,
        trace_id: str,
        span_id: str,
        parent: str | None,
        service: str,
        version: str,
        name: str,
        kind: SpanKind,
        start: int,
        end: int,
        error: bool,
        http_status: int | None = None,
        route: str | None = None,
    ) -> None:
        self.spans.append(
            Span(
                tenant_id=self.s.tenant_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent,
                service=service,
                service_version=version,
                environment=self.s.environment,
                name=name,
                kind=kind,
                start_ns=start,
                end_ns=end,
                status_code=StatusCode.ERROR if error else StatusCode.UNSET,
                http_route=route,
                http_status=http_status,
            )
        )

    def serve(self, service: str, start: int, trace_id: str, parent: str | None) -> tuple[int, int]:
        """Simulate one inbound request; returns (end_ns, http_status)."""
        behaviour = (self.s.after if start >= self.t_ns else self.s.before)[service]
        version = self._version(service, start)
        server_id = self._span_id()
        overhead = self._ms(0.2)

        own_ms = behaviour.own_latency_ms * math.exp(behaviour.sigma * self.rng.gauss(0.0, 1.0))
        own_ms += behaviour.extra_latency_ms
        own_fails = self.rng.random() < behaviour.error_rate
        own_start = start + overhead
        own_end = own_start + self._ms(own_ms)
        self._emit(
            trace_id, self._span_id(), server_id, service, version, f"{service}.own_work",
            SpanKind.INTERNAL, own_start, own_end, own_fails,
        )  # fmt: skip

        status = 200
        if own_fails:
            status = 500
            end = own_end + overhead
        else:
            calls = CALLS[service]
            net = self._ms(self.s.network_ms)
            results: list[tuple[int, int]] = []  # (client end, callee status)
            for target in calls:
                client_id = self._span_id()
                arrival = own_end + net
                if arrival >= self.t_ns and target in self.s.down_after_deploy:
                    # connection refused: no callee span, no HTTP status, the call itself fails
                    client_end = own_end + self._ms(CONNECT_FAIL_MS)
                    self._emit(
                        trace_id, client_id, server_id, service, version,
                        f"{service} -> {target}", SpanKind.CLIENT, own_end, client_end, True,
                    )  # fmt: skip
                    results.append((client_end, 503))
                    continue
                child_end, child_status = self.serve(target, arrival, trace_id, client_id)
                client_end = child_end + net
                failed = child_status >= 500
                self._emit(
                    trace_id, client_id, server_id, service, version, f"{service} -> {target}",
                    SpanKind.CLIENT, own_end, client_end, failed, http_status=child_status,
                )  # fmt: skip
                results.append((client_end, child_status))
            failures = [end for end, code in results if code >= 500]
            if failures:
                status = 502
                end = min(failures) + overhead  # first failure aborts the request (gather)
            elif results:
                end = max(end for end, _ in results) + overhead
            else:
                end = own_end + overhead
        self._emit(
            trace_id, server_id, parent, service, version, f"{ROUTES[service]}",
            SpanKind.SERVER, start, end, status >= 500, http_status=status,
            route=ROUTES[service],
        )  # fmt: skip
        return end, status


def generate(scenario: Scenario, seed: int, deployed_at: datetime = T_DEPLOY) -> list[Span]:
    """All spans of the scenario, Poisson arrivals at ``rate_per_minute``."""
    rng = random.Random(seed)
    t_ns = datetime_to_ns(deployed_at)
    builder = _Builder(scenario, t_ns, rng)
    rate_per_ns = scenario.rate_per_minute / (60 * NS_PER_S)
    now = t_ns + round(scenario.start_minutes * 60 * NS_PER_S)
    stop = t_ns + round(scenario.end_minutes * 60 * NS_PER_S)
    while True:
        now += round(rng.expovariate(rate_per_ns))
        if now > stop:
            break
        builder.serve("gateway", now, builder.trace_id(), None)
    return builder.spans


# ----------------------------------------------------------------------------- hand-made spans
def make_span(
    span_id: str,
    parent: str | None,
    service: str,
    kind: SpanKind,
    start_ms: float,
    end_ms: float,
    *,
    trace_id: str = "t1",
    error: bool = False,
    http_status: int | None = None,
    tenant_id: str = "tenant-a",
    environment: str | None = "demo",
    base_ns: int = 0,
) -> Span:
    """A span with millisecond offsets from ``base_ns`` (for small hand-built topologies)."""
    return Span(
        tenant_id=tenant_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        service_version="v1",
        environment=environment,
        name=f"{service}:{span_id}",
        kind=kind,
        start_ns=base_ns + round(start_ms * NS_PER_MS),
        end_ns=base_ns + round(end_ms * NS_PER_MS),
        status_code=StatusCode.ERROR if error else StatusCode.UNSET,
        http_status=http_status,
    )
