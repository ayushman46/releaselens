"""Per-service request samples and window statistics.

**Requests are SERVER spans.** A service's requests in a window are its SERVER spans whose
*start* falls in the window. For each request the engine keeps:

* ``latency_ms``   — the SERVER span's duration;
* ``self_time_ms`` — its *service* self-time (:func:`rl_core.selftime.service_self_time_ns`);
* ``is_error``     — :attr:`rl_core.telemetry.model.Span.is_error`;
* ``is_own_error`` — the error did not come from a failing callee
  (:meth:`rl_core.telemetry.index.TraceIndex.is_own_error`).

Latency and self-time arrays are aligned (index ``i`` is the same request), so both distributions
always have the same sample size and the same window membership.

**Percentiles** use ``numpy.quantile`` with its default ``method="linear"`` (Hyndman & Fan type 7:
linear interpolation between the two nearest order statistics). The bootstrap in
:mod:`rl_core.stats` uses the same estimator, so point estimates and intervals agree.

Throughput is ``request_count / window minutes``. Window length is the measure of the interval
(an inclusive end adds no length).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from rl_core.selftime import service_self_time_ns
from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import NS_PER_MS, NS_PER_S, Span, SpanKind

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]


@dataclass(frozen=True, slots=True, eq=False)
class ServiceSamples:
    """Every request (SERVER span) of one service, columnar, sorted by start time (ties by
    ``(trace_id, span_id)``, so the order never depends on the order spans arrived in)."""

    service: str
    start_ns: IntArray
    latency_ms: FloatArray
    self_time_ms: FloatArray
    is_error: BoolArray
    is_own_error: BoolArray

    def __len__(self) -> int:
        return int(self.start_ns.size)

    def select(self, lo_ns: int, hi_ns: int, *, hi_inclusive: bool) -> ServiceSamples:
        """Requests starting in ``[lo_ns, hi_ns)`` (or ``[lo_ns, hi_ns]``)."""
        lo = int(np.searchsorted(self.start_ns, lo_ns, side="left"))
        hi = int(np.searchsorted(self.start_ns, hi_ns, side="right" if hi_inclusive else "left"))
        hi = max(hi, lo)
        return ServiceSamples(
            service=self.service,
            start_ns=self.start_ns[lo:hi],
            latency_ms=self.latency_ms[lo:hi],
            self_time_ms=self.self_time_ms[lo:hi],
            is_error=self.is_error[lo:hi],
            is_own_error=self.is_own_error[lo:hi],
        )

    @staticmethod
    def empty(service: str) -> ServiceSamples:
        return ServiceSamples(
            service=service,
            start_ns=np.empty(0, dtype=np.int64),
            latency_ms=np.empty(0, dtype=np.float64),
            self_time_ms=np.empty(0, dtype=np.float64),
            is_error=np.empty(0, dtype=np.bool_),
            is_own_error=np.empty(0, dtype=np.bool_),
        )


def build_service_samples(
    index: TraceIndex,
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> dict[str, ServiceSamples]:
    """Request samples per service for SERVER spans starting in ``[start_ns, end_ns]``.

    Self-time and own-error attribution look at each request's descendants in the *whole* index,
    so a request near the end of the range keeps children that start after ``end_ns``.
    """
    rows: dict[str, list[tuple[int, int, int, bool, bool, str, str]]] = {}
    for span in index:
        if span.kind is not SpanKind.SERVER:
            continue
        if start_ns is not None and span.start_ns < start_ns:
            continue
        if end_ns is not None and span.start_ns > end_ns:
            continue
        rows.setdefault(span.service, []).append(
            (
                span.start_ns,
                span.duration_ns,
                service_self_time_ns(index, span),
                span.is_error,
                index.is_own_error(span),
                span.trace_id,
                span.span_id,
            )
        )
    out: dict[str, ServiceSamples] = {}
    for service, items in rows.items():
        # Total order: start time, then span identity. Exporters often truncate timestamps to the
        # microsecond or millisecond, so equal start times are common; ordering ties by input
        # order would make the bootstrap (which resamples by array position) depend on the order
        # in which spans were delivered or queried.
        items.sort(key=lambda r: (r[0], r[5], r[6]))
        out[service] = ServiceSamples(
            service=service,
            start_ns=np.fromiter((r[0] for r in items), dtype=np.int64, count=len(items)),
            latency_ms=np.fromiter((r[1] for r in items), dtype=np.float64, count=len(items))
            / NS_PER_MS,
            self_time_ms=np.fromiter((r[2] for r in items), dtype=np.float64, count=len(items))
            / NS_PER_MS,
            is_error=np.fromiter((r[3] for r in items), dtype=np.bool_, count=len(items)),
            is_own_error=np.fromiter((r[4] for r in items), dtype=np.bool_, count=len(items)),
        )
    return out


def quantile(values: FloatArray, q: float) -> float | None:
    """``numpy.quantile`` (linear method) or ``None`` for an empty sample."""
    if values.size == 0:
        return None
    return float(np.quantile(values, q))


@dataclass(frozen=True, slots=True, eq=False)
class WindowStats:
    """Request statistics of one service in one window."""

    service: str
    start_ns: int
    end_ns: int
    end_inclusive: bool
    samples: ServiceSamples

    @property
    def duration_minutes(self) -> float:
        return max(0, self.end_ns - self.start_ns) / (60 * NS_PER_S)

    @property
    def request_count(self) -> int:
        return len(self.samples)

    @property
    def error_count(self) -> int:
        return int(np.count_nonzero(self.samples.is_error))

    @property
    def own_error_count(self) -> int:
        return int(np.count_nonzero(self.samples.is_own_error))

    @property
    def error_rate(self) -> float | None:
        n = self.request_count
        return None if n == 0 else self.error_count / n

    @property
    def own_error_rate(self) -> float | None:
        n = self.request_count
        return None if n == 0 else self.own_error_count / n

    @property
    def throughput_rpm(self) -> float | None:
        minutes = self.duration_minutes
        return None if minutes <= 0 else self.request_count / minutes

    @property
    def latency_ms(self) -> FloatArray:
        return self.samples.latency_ms

    @property
    def self_time_ms(self) -> FloatArray:
        return self.samples.self_time_ms

    def latency_quantile(self, q: float) -> float | None:
        return quantile(self.samples.latency_ms, q)

    def self_time_quantile(self, q: float) -> float | None:
        return quantile(self.samples.self_time_ms, q)

    @property
    def p50(self) -> float | None:
        return self.latency_quantile(0.5)

    @property
    def p95(self) -> float | None:
        return self.latency_quantile(0.95)

    @property
    def p99(self) -> float | None:
        return self.latency_quantile(0.99)


def window_stats(
    samples: ServiceSamples,
    start_ns: int,
    end_ns: int,
    *,
    end_inclusive: bool,
) -> WindowStats:
    return WindowStats(
        service=samples.service,
        start_ns=start_ns,
        end_ns=end_ns,
        end_inclusive=end_inclusive,
        samples=samples.select(start_ns, end_ns, hi_inclusive=end_inclusive),
    )


def compute_window_stats(
    spans: Iterable[Span] | TraceIndex,
    start_ns: int,
    end_ns: int,
    *,
    end_inclusive: bool = False,
) -> dict[str, WindowStats]:
    """Window statistics for every service with at least one SERVER span in the window."""
    index = spans if isinstance(spans, TraceIndex) else TraceIndex.build(spans)
    per_service = build_service_samples(index, start_ns=start_ns, end_ns=end_ns)
    return {
        service: window_stats(samples, start_ns, end_ns, end_inclusive=end_inclusive)
        for service, samples in sorted(per_service.items())
    }
