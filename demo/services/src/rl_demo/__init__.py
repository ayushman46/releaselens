"""ReleaseLens demo system.

Topology (spec §11.1)::

    k6 ─► gateway ─► orders ─┬─► inventory
                             └─► payment ─► fraud

Every service is a real FastAPI process emitting real OpenTelemetry spans. A service's *own*
work is simulated with a configurable latency distribution and error rate; regressions (e.g.
``payment:v2`` adds +250 ms and ~3.5% errors) are switched on purely through environment
variables, so a demo run is reproducible. Nothing here fabricates telemetry: spans are produced
by actually executing requests.
"""

__version__ = "0.1.0"
