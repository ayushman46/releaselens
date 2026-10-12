"""The span model the regression engine reasons about.

This mirrors the subset of an OpenTelemetry span that post-deployment analysis needs. Times are
integer nanoseconds since the Unix epoch (CONVENTIONS §5), never floats, so interval arithmetic
in self-time computation is exact.

A span is identified by ``(trace_id, span_id)`` (:data:`SpanKey`): OTel only guarantees span ids
to be unique *within* a trace, so every index in ``rl_core`` keys on the pair.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

SpanKey = tuple[str, str]
"""``(trace_id, span_id)`` — the identity of a span."""


def datetime_to_ns(value: datetime) -> int:
    """Exact conversion of a timezone-aware datetime to Unix-epoch nanoseconds.

    ``datetime.timestamp()`` goes through a float and loses sub-microsecond exactness for current
    dates; integer timedelta arithmetic does not.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * NS_PER_S + delta.microseconds * 1_000


def ns_to_datetime(value: int) -> datetime:
    """Unix-epoch nanoseconds → UTC datetime (truncated to microseconds, datetime's resolution)."""
    return _EPOCH + timedelta(microseconds=value // 1_000)


class SpanKind(StrEnum):
    """OTel span kind. SERVER spans are the request samples; CLIENT/PRODUCER are outbound waits."""

    SERVER = "SERVER"
    CLIENT = "CLIENT"
    INTERNAL = "INTERNAL"
    PRODUCER = "PRODUCER"
    CONSUMER = "CONSUMER"


class StatusCode(StrEnum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


def _empty_attributes() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Span:
    """One immutable span.

    ``attributes`` is excluded from hashing (mappings are not hashable) but still compared.
    """

    tenant_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    service_version: str | None
    environment: str | None
    name: str
    kind: SpanKind
    start_ns: int
    end_ns: int
    status_code: StatusCode = StatusCode.UNSET
    http_route: str | None = None
    http_status: int | None = None
    attributes: Mapping[str, object] = field(default_factory=_empty_attributes, hash=False)

    def __post_init__(self) -> None:
        # Fail loudly on structurally impossible spans: a negative duration or a span that is its
        # own parent would silently corrupt self-time and graph construction downstream.
        if self.end_ns < self.start_ns:
            raise ValueError(
                f"span {self.span_id} ends before it starts ({self.end_ns} < {self.start_ns})"
            )
        if not self.trace_id or not self.span_id:
            raise ValueError("trace_id and span_id must be non-empty")
        if self.parent_span_id is not None and self.parent_span_id == self.span_id:
            raise ValueError(f"span {self.span_id} cannot be its own parent")
        if not self.service:
            raise ValueError("service must be non-empty")

    @property
    def key(self) -> SpanKey:
        return (self.trace_id, self.span_id)

    @property
    def parent_key(self) -> SpanKey | None:
        if self.parent_span_id is None:
            return None
        return (self.trace_id, self.parent_span_id)

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def duration_ms(self) -> float:
        return self.duration_ns / NS_PER_MS

    @property
    def is_error(self) -> bool:
        """Status ERROR, or an HTTP 5xx answered by this span as a SERVER.

        Only SERVER spans use the HTTP status: a 5xx on a SERVER span is that server's failure,
        whereas a 5xx on a CLIENT span reports the *callee's* answer, which the callee's own
        SERVER span already records. A CLIENT span is an error only when its instrumentation set
        status ERROR.
        """
        if self.status_code is StatusCode.ERROR:
            return True
        return (
            self.kind is SpanKind.SERVER
            and self.http_status is not None
            and self.http_status >= 500
        )
