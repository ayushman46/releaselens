"""Telemetry domain model (spans) and trace-structure helpers used by the regression engine.

* :mod:`rl_core.telemetry.model` — the immutable :class:`~rl_core.telemetry.model.Span`
* :mod:`rl_core.telemetry.index` — per-trace parent/child index, local subtrees, own errors
"""

from rl_core.telemetry.model import (
    Span,
    SpanKey,
    SpanKind,
    StatusCode,
    datetime_to_ns,
    ns_to_datetime,
)

__all__ = ["Span", "SpanKey", "SpanKind", "StatusCode", "datetime_to_ns", "ns_to_datetime"]
