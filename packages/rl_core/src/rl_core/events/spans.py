"""``telemetry.spans`` messages: batches of spans of one service, produced by the OTLP ingest.

Wire format (one JSON object per Kafka message, key ``"<tenant_id>:<service.name>"``)::

    {"schema_version": 1, "type": "telemetry.span_batch", "batch_id": "<uuid>",
     "tenant_id": "<uuid>", "service": "payment", "received_at": "2026-10-13T12:00:00Z",
     "spans": [{"trace_id": "<32 hex>", "span_id": "<16 hex>", "parent_span_id": null,
                "service_version": "v2", "environment": "demo", "name": "/charge",
                "kind": "SERVER", "start_ns": 1760356800000000000,
                "end_ns": 1760356800061000000, "status_code": "UNSET",
                "http_route": "/charge", "http_status": 200, "attributes": {...}}]}

* ``tenant_id`` is set by the API from the authenticated key, never from telemetry content.
* Every span of a message belongs to ``service`` (the resource's ``service.name``). Version and
  environment stay per span: one export can carry two versions of a service mid-rollout.
* The API splits a service's spans into several messages when one would exceed
  :data:`MAX_SPAN_BATCH_BYTES`, so every message fits Kafka's default 1 MB limit.

The ingest has already validated and bounded every field. The consumer validates again
(:func:`parse_span_batch`), because a topic is not a trust boundary it controls: a message that
fails is a permanent error and goes to the DLQ whole.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from rl_core.events.deployment import EnvironmentName, ServiceName
from rl_core.events.errors import (
    EventTooLargeError,
    EventValidationError,
    MalformedEventError,
    UnknownEventTypeError,
    UnknownSchemaVersionError,
)
from rl_core.ids import uuid7
from rl_core.telemetry.model import Span, SpanKind, StatusCode

EVENT_TYPE_SPAN_BATCH: Final = "telemetry.span_batch"
SPAN_BATCH_SCHEMA_VERSION: Final = 1

#: Upper bound of one encoded message. librdkafka's default ``message.max.bytes`` is 1 000 000
#: and the broker's is 1 048 588; the margin covers headers and the record overhead.
MAX_SPAN_BATCH_BYTES: Final = 768 * 1024
#: Hard cap on spans per message (a sanity bound; the byte limit normally binds first).
MAX_SPANS_PER_MESSAGE: Final = 20_000

# Field limits (the ingest truncates or drops to fit them; the columns in rl_db are sized from
# them, so anything the parser accepts fits).
MAX_SPAN_NAME_LEN: Final = 512
MAX_HTTP_ROUTE_LEN: Final = 512
MAX_VERSION_LEN: Final = 200
MAX_ATTRIBUTES: Final = 64
MAX_ATTRIBUTE_KEY_LEN: Final = 128
MAX_ATTRIBUTE_VALUE_LEN: Final = 256
MAX_ATTRIBUTE_ARRAY_LEN: Final = 16
#: Budget for one span's attributes (sum of key and value lengths, in characters).
MAX_ATTRIBUTES_CHARS: Final = 16 * 1024

TRACE_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE: Final = re.compile(r"^[0-9a-f]{16}$")
_ZERO_TRACE: Final = "0" * 32
_ZERO_SPAN: Final = "0" * 16
_MAX_INT64: Final = 2**63 - 1

TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
SpanId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
Scalar = str | bool | int | float
AttributeValue = Scalar | list[Scalar]


class WireSpan(BaseModel):
    """One span as carried on ``telemetry.spans``."""

    # NaN/inf cannot be stored in jsonb; the ingest never produces them.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    trace_id: TraceId
    span_id: SpanId
    parent_span_id: SpanId | None = None
    service_version: Annotated[str, StringConstraints(max_length=MAX_VERSION_LEN)] | None = None
    environment: EnvironmentName | None = None
    name: Annotated[str, StringConstraints(max_length=MAX_SPAN_NAME_LEN)]
    kind: SpanKind
    start_ns: Annotated[int, Field(ge=1, le=_MAX_INT64)]
    end_ns: Annotated[int, Field(ge=1, le=_MAX_INT64)]
    status_code: StatusCode = StatusCode.UNSET
    http_route: Annotated[str, StringConstraints(max_length=MAX_HTTP_ROUTE_LEN)] | None = None
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    attributes: Annotated[dict[str, AttributeValue], Field(max_length=MAX_ATTRIBUTES)] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.end_ns < self.start_ns:
            raise ValueError("end_ns precedes start_ns")
        if self.trace_id == _ZERO_TRACE or self.span_id == _ZERO_SPAN:
            raise ValueError("all-zero trace_id/span_id is invalid")
        if self.parent_span_id is not None and self.parent_span_id in (_ZERO_SPAN, self.span_id):
            raise ValueError("invalid parent_span_id")
        return self


class SpanBatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    type: Literal["telemetry.span_batch"]
    batch_id: uuid.UUID
    tenant_id: uuid.UUID
    service: ServiceName
    received_at: AwareDatetime
    spans: Annotated[list[WireSpan], Field(min_length=1, max_length=MAX_SPANS_PER_MESSAGE)]

    def to_spans(self) -> list[Span]:
        """The batch as rl_core :class:`~rl_core.telemetry.model.Span` objects."""
        return [to_span(str(self.tenant_id), self.service, w) for w in self.spans]


def span_to_wire(span: Span) -> dict[str, Any]:
    """The wire form of an rl_core span (its tenant and service travel in the envelope)."""
    return {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "service_version": span.service_version,
        "environment": span.environment,
        "name": span.name,
        "kind": span.kind.value,
        "start_ns": span.start_ns,
        "end_ns": span.end_ns,
        "status_code": span.status_code.value,
        "http_route": span.http_route,
        "http_status": span.http_status,
        "attributes": dict(span.attributes),
    }


def to_span(tenant_id: str, service: str, wire: WireSpan) -> Span:
    return Span(
        tenant_id=tenant_id,
        trace_id=wire.trace_id,
        span_id=wire.span_id,
        parent_span_id=wire.parent_span_id,
        service=service,
        service_version=wire.service_version,
        environment=wire.environment,
        name=wire.name,
        kind=wire.kind,
        start_ns=wire.start_ns,
        end_ns=wire.end_ns,
        status_code=wire.status_code,
        http_route=wire.http_route,
        http_status=wire.http_status,
        attributes=MappingProxyType(dict(wire.attributes)),
    )


# ----------------------------------------------------------------------------- encoding
def _head(tenant_id: uuid.UUID, service: str, received_at: datetime) -> bytes:
    head = {
        "schema_version": SPAN_BATCH_SCHEMA_VERSION,
        "type": EVENT_TYPE_SPAN_BATCH,
        "batch_id": str(uuid7()),
        "tenant_id": str(tenant_id),
        "service": service,
        "received_at": received_at.astimezone(UTC).isoformat(),
    }
    # The head is a JSON object without its closing brace; "spans" is appended to it.
    return json.dumps(head, separators=(",", ":"))[:-1].encode() + b',"spans":['


def encode_span(span: Mapping[str, Any]) -> bytes:
    """One wire span as compact UTF-8 JSON (no NaN/inf: the ingest never produces them)."""
    return json.dumps(span, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def encode_span_batches(
    *,
    tenant_id: uuid.UUID,
    service: str,
    spans: Iterable[bytes],
    received_at: datetime,
    max_bytes: int = MAX_SPAN_BATCH_BYTES,
) -> Iterator[bytes]:
    """Pack already-encoded spans (:func:`encode_span`) into as few messages as fit.

    Each message is a complete envelope of at most ``max_bytes``. A single span that cannot fit
    an envelope on its own raises :class:`EventTooLargeError` (the ingest's field limits make
    that impossible for spans it built).
    """
    head = _head(tenant_id, service, received_at)
    tail = b"]}"
    budget = max_bytes - len(head) - len(tail)
    chunk: list[bytes] = []
    size = 0
    for encoded in spans:
        if len(encoded) > budget:
            raise EventTooLargeError(f"one span encodes to {len(encoded)} bytes")
        extra = len(encoded) + (1 if chunk else 0)
        if chunk and (size + extra > budget or len(chunk) >= MAX_SPANS_PER_MESSAGE):
            yield head + b",".join(chunk) + tail
            head = _head(tenant_id, service, received_at)  # fresh batch_id per message
            chunk, size = [], 0
            extra = len(encoded)
        chunk.append(encoded)
        size += extra
    if chunk:
        yield head + b",".join(chunk) + tail


# ----------------------------------------------------------------------------- decoding
def _classify_failure(data: bytes) -> None:
    """Raise the most specific permanent error for a message that failed validation."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise MalformedEventError("event is not valid UTF-8") from exc
    except (ValueError, RecursionError) as exc:
        raise MalformedEventError(f"event is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedEventError("event must be a JSON object")
    event_type = obj.get("type")
    if event_type != EVENT_TYPE_SPAN_BATCH:
        raise UnknownEventTypeError(f"unknown event type {str(event_type)[:100]!r}")
    version = obj.get("schema_version")
    if isinstance(version, bool) or version != SPAN_BATCH_SCHEMA_VERSION:
        raise UnknownSchemaVersionError(f"unsupported schema_version {str(version)[:20]!r}")


def parse_span_batch(raw: bytes | bytearray | memoryview | None) -> SpanBatchEnvelope:
    """Validate one ``telemetry.spans`` message. Raises a PermanentError subclass on bad input."""
    if raw is None:
        raise MalformedEventError("event has no value (tombstone)")
    data = bytes(raw)
    if len(data) > MAX_SPAN_BATCH_BYTES:
        raise EventTooLargeError(f"event is {len(data)} bytes; limit is {MAX_SPAN_BATCH_BYTES}")
    try:
        return SpanBatchEnvelope.model_validate_json(data)
    except ValidationError as exc:
        _classify_failure(data)
        errors = [
            {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"], "msg": e["msg"]}
            for e in exc.errors(include_url=False, include_input=False)
        ]
        first = errors[0] if errors else {"loc": "?", "msg": "invalid"}
        raise EventValidationError(
            f"{len(errors)} validation error(s); first: {first['loc']}: {first['msg']}", errors
        ) from None


def is_valid_trace_id(value: str) -> bool:
    return bool(TRACE_ID_RE.fullmatch(value)) and value != _ZERO_TRACE


def is_valid_span_id(value: str) -> bool:
    return bool(SPAN_ID_RE.fullmatch(value)) and value != _ZERO_SPAN
