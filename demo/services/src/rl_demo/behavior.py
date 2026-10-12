"""Configurable "own work" of a demo service: latency + error injection.

Environment (per container):

=================================  ==========================================================
``RL_DEMO_SERVICE``                which service this process is (gateway, orders, …)
``RL_DEMO_VERSION``                service.version resource attribute (e.g. ``v1``, ``v2``)
``RL_DEMO_ENVIRONMENT``            deployment.environment (default ``demo``)
``RL_DEMO_OWN_LATENCY_MS``         median own-work latency (lognormal), default per service
``RL_DEMO_OWN_LATENCY_SIGMA``      lognormal sigma (spread), default 0.25
``RL_DEMO_EXTRA_LATENCY_MS``       regression: constant extra own-work latency (default 0)
``RL_DEMO_ERROR_RATE``             probability that own work fails (0..1, default 0)
``RL_DEMO_SEED``                   RNG seed (default 0) → reproducible sequence per process
=================================  ==========================================================
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import threading
from dataclasses import dataclass

DEFAULT_OWN_LATENCY_MS: dict[str, float] = {
    "gateway": 4.0,
    "orders": 12.0,
    "inventory": 18.0,
    "payment": 60.0,
    "fraud": 35.0,
}

SERVICES = tuple(DEFAULT_OWN_LATENCY_MS)


class InjectedError(RuntimeError):
    """Raised when the configured error rate decides this request's own work fails."""


@dataclass(frozen=True, slots=True)
class Behavior:
    service: str
    version: str
    environment: str
    own_latency_ms: float
    sigma: float
    extra_latency_ms: float
    error_rate: float
    seed: int

    @staticmethod
    def from_env() -> Behavior:
        service = os.environ.get("RL_DEMO_SERVICE", "")
        if service not in DEFAULT_OWN_LATENCY_MS:
            raise ValueError(f"RL_DEMO_SERVICE must be one of {SERVICES}, got {service!r}")
        b = Behavior(
            service=service,
            version=os.environ.get("RL_DEMO_VERSION", "v1"),
            environment=os.environ.get("RL_DEMO_ENVIRONMENT", "demo"),
            own_latency_ms=float(
                os.environ.get("RL_DEMO_OWN_LATENCY_MS", DEFAULT_OWN_LATENCY_MS[service])
            ),
            sigma=float(os.environ.get("RL_DEMO_OWN_LATENCY_SIGMA", "0.25")),
            extra_latency_ms=float(os.environ.get("RL_DEMO_EXTRA_LATENCY_MS", "0")),
            error_rate=float(os.environ.get("RL_DEMO_ERROR_RATE", "0")),
            seed=int(os.environ.get("RL_DEMO_SEED", "0")),
        )
        if not 0.0 <= b.error_rate <= 1.0:
            raise ValueError("RL_DEMO_ERROR_RATE must be within [0, 1]")
        if b.own_latency_ms < 0 or b.extra_latency_ms < 0 or b.sigma < 0:
            raise ValueError("latencies and sigma must be non-negative")
        return b


class OwnWork:
    """Thread-safe seeded source of own-work durations and failure decisions."""

    def __init__(self, behavior: Behavior) -> None:
        self.b = behavior
        self._rng = random.Random(behavior.seed)  # noqa: S311 - simulation, not security
        self._lock = threading.Lock()

    def draw(self) -> tuple[float, bool]:
        """Return (latency_ms, fails)."""
        with self._lock:
            z = self._rng.gauss(0.0, 1.0)
            fails = self._rng.random() < self.b.error_rate
        base = self.b.own_latency_ms * math.exp(self.b.sigma * z) if self.b.own_latency_ms else 0.0
        return base + self.b.extra_latency_ms, fails

    async def perform(self) -> float:
        """Spend the own-work time (non-blocking) and raise InjectedError if it fails."""
        latency_ms, fails = self.draw()
        await asyncio.sleep(latency_ms / 1000.0)
        if fails:
            raise InjectedError(f"{self.b.service} {self.b.version}: injected failure")
        return latency_ms
