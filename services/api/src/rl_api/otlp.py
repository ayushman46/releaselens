"""OTLP/HTTP trace decoding: request bytes → validated, bounded wire spans grouped by service.

Two encodings of ``ExportTraceServiceRequest`` are accepted (OTLP 1.x, ``/v1/traces``):

* ``application/x-protobuf`` — the binary protobuf message (what the Collector sends);
* ``application/json`` — OTLP/JSON: the protobuf JSON mapping, except that ``traceId``,
  ``spanId`` and ``parentSpanId`` are **hex** strings rather than base64, enums are integers
  (names are tolerated) and 64-bit integers may be strings. Field names may be lowerCamelCase or
  snake_case. Unknown fields are ignored, as the spec requires of receivers.

**Mapping to ReleaseLens spans** (``rl_core.events.spans.WireSpan``):

==========================  ============================================================
tenant                      the API key's tenant, **only** (resource attributes are never read
                            for it)
service / version / env     resource ``service.name`` (default ``unknown_service``),
                            ``service.version``, ``deployment.environment.name`` or the older
                            ``deployment.environment``
kind                        SPAN_KIND_UNSPECIFIED/INTERNAL → INTERNAL, SERVER, CLIENT,
                            PRODUCER, CONSUMER
status_code                 STATUS_CODE_ERROR → ERROR, OK → OK, otherwise UNSET
http_route / http_status    span attributes ``http.route``; ``http.response.status_code``,
                            else the legacy ``http.status_code`` (100-599, else dropped)
attributes                  span attributes, bounded: at most 64, keys ≤ 128 chars, strings
                            truncated to 256 chars, arrays of scalars truncated to 16 items,
                            nested maps / bytes / non-finite floats dropped, ≤ 16 KiB in total
==========================  ============================================================

**Rejected spans** (reported through OTLP ``partial_success``, the rest is accepted): invalid
trace or span id (wrong length, all zeros), a parent id equal to the span id, end before start,
a zero start time, start more than ``max_future`` ahead of the server clock or older than
``max_age``, an unknown span kind, and every span of a resource whose ``service.name``,
``service.version`` or environment is not a valid identifier.
"""

from __future__ import annotations

import base64
import json
import math
import re
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

from rl_core.events import spans as limits
from rl_core.events.spans import encode_span

Encoding = Literal["protobuf", "json"]

DEFAULT_SERVICE: Final = "unknown_service"
_SERVICE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
_ENVIRONMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_RE: Final = re.compile(r"^[!-~]{1,200}$")
_MAX_INT64: Final = 2**63 - 1
_ZERO8: Final = bytes(8)
_ZERO16: Final = bytes(16)

_KINDS: Final = {
    0: "INTERNAL",  # SPAN_KIND_UNSPECIFIED: the spec says to treat it as INTERNAL
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}
_STATUS: Final = {1: "OK", 2: "ERROR"}

_ID_FIELDS: Final = ("traceId", "trace_id", "spanId", "span_id", "parentSpanId", "parent_span_id")


class OtlpDecodeError(ValueError):
    """The request body is not a decodable ExportTraceServiceRequest (HTTP 400)."""


class PayloadTooLargeError(ValueError):
    """The body, or what it decompresses to, exceeds a configured limit (HTTP 413)."""


# ----------------------------------------------------------------------------- bytes
def gunzip_bounded(data: bytes, limit: int) -> bytes:
    """Decompress gzip (all members) without ever holding more than ``limit + 1`` output bytes.

    A small body that inflates enormously (a "gzip bomb") is refused as soon as the output passes
    ``limit``; nothing beyond that is ever materialised.
    """
    out = bytearray()
    remaining = data
    try:
        while remaining:
            inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            pending = remaining
            while True:
                out += inflater.decompress(pending, limit - len(out) + 1)
                if len(out) > limit:
                    raise PayloadTooLargeError(f"decompressed body exceeds {limit} bytes")
                pending = inflater.unconsumed_tail
                if not pending:
                    break
            if not inflater.eof:
                raise OtlpDecodeError("truncated gzip body")
            remaining = inflater.unused_data
    except zlib.error as exc:
        raise OtlpDecodeError(f"invalid gzip body: {exc}") from exc
    return bytes(out)


def _hex_to_b64(value: Any) -> str:
    """OTLP/JSON hex id → the base64 the protobuf JSON mapping expects; '' when not hex (the span
    is then rejected for an invalid id rather than the whole request)."""
    if not isinstance(value, str):
        return ""
    try:
        return base64.b64encode(bytes.fromhex(value)).decode()
    except ValueError:
        return ""


def _fix_json_ids(obj: Any) -> None:
    for resource_spans in _list(obj, "resourceSpans", "resource_spans"):
        for scope_spans in _list(resource_spans, "scopeSpans", "scope_spans"):
            for span in _list(scope_spans, "spans"):
                if not isinstance(span, dict):
                    continue
                for name in _ID_FIELDS:
                    if name in span:
                        span[name] = _hex_to_b64(span[name])
                # Not stored; dropping them also skips their (hex-encoded) ids.
                span.pop("links", None)
                span.pop("events", None)


def _list(obj: Any, *names: str) -> Iterator[Any]:
    if not isinstance(obj, dict):
        return
    for name in names:
        value = obj.get(name)
        if isinstance(value, list):
            yield from value


def decode_request(body: bytes, encoding: Encoding) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    if encoding == "protobuf":
        try:
            request.ParseFromString(body)
        except DecodeError as exc:
            raise OtlpDecodeError("body is not a valid ExportTraceServiceRequest") from exc
        return request
    try:
        obj = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise OtlpDecodeError("body is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise OtlpDecodeError("body must be a JSON object")
    _fix_json_ids(obj)
    try:
        json_format.ParseDict(obj, request, ignore_unknown_fields=True)
    except (json_format.ParseError, ValueError, TypeError, RecursionError) as exc:
        raise OtlpDecodeError(f"body is not a valid OTLP/JSON trace request: {exc}") from exc
    return request


def encode_response(rejected: int, message: str, encoding: Encoding) -> tuple[bytes, str]:
    """``ExportTraceServiceResponse`` in the request's encoding (partial_success when needed)."""
    response = ExportTraceServiceResponse()
    if rejected:
        response.partial_success.rejected_spans = rejected
        response.partial_success.error_message = message
    if encoding == "protobuf":
        return response.SerializeToString(), "application/x-protobuf"
    return json_format.MessageToJson(response, indent=None).encode(), "application/json"


# ----------------------------------------------------------------------------- mapping
def _clean(text: str, limit: int) -> str:
    # Postgres text and jsonb cannot hold NUL characters.
    return text.replace("\x00", "�")[:limit]


def _scalar(value: AnyValue) -> str | bool | int | float | None:
    which = value.WhichOneof("value")
    if which == "string_value":
        return _clean(value.string_value, limits.MAX_ATTRIBUTE_VALUE_LEN)
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return int(value.int_value)
    if which == "double_value":
        number = value.double_value
        return number if math.isfinite(number) else None
    return None


def _attribute(value: AnyValue) -> Any:
    if value.WhichOneof("value") == "array_value":
        items = [_scalar(v) for v in value.array_value.values[: limits.MAX_ATTRIBUTE_ARRAY_LEN]]
        return [v for v in items if v is not None]
    return _scalar(value)


def _string_attr(value: AnyValue | None) -> str | None:
    if value is None or value.WhichOneof("value") != "string_value":
        return None
    return value.string_value


def _status_code(value: AnyValue | None) -> int | None:
    if value is None:
        return None
    which = value.WhichOneof("value")
    code: int | None = None
    if which == "int_value":
        code = int(value.int_value)
    elif which == "string_value" and value.string_value.isdigit():
        code = int(value.string_value)
    elif which == "double_value" and value.double_value.is_integer():
        code = int(value.double_value)
    return code if code is not None and 100 <= code <= 599 else None


@dataclass
class ConvertedRequest:
    """Accepted spans as encoded wire spans, grouped by service; rejection reasons counted."""

    by_service: dict[str, list[bytes]] = field(default_factory=dict)
    accepted: int = 0
    rejections: Counter[str] = field(default_factory=Counter)

    @property
    def rejected(self) -> int:
        return sum(self.rejections.values())

    def error_message(self, max_len: int = 1000) -> str:
        text = "; ".join(f"{n} span(s): {why}" for why, n in sorted(self.rejections.items()))
        return text[:max_len]


@dataclass(frozen=True, slots=True)
class TimeBounds:
    """Accepted span start times: ``[now - max_age, now + max_future]`` (nanoseconds)."""

    now_ns: int
    max_future_ns: int
    max_age_ns: int


def convert_request(request: ExportTraceServiceRequest, bounds: TimeBounds) -> ConvertedRequest:
    out = ConvertedRequest()
    earliest = bounds.now_ns - bounds.max_age_ns
    latest = bounds.now_ns + bounds.max_future_ns
    for resource_spans in request.resource_spans:
        resource = {kv.key: kv.value for kv in resource_spans.resource.attributes}
        n_spans = sum(len(ss.spans) for ss in resource_spans.scope_spans)
        if not n_spans:
            continue
        service_attr = resource.get("service.name")
        service = _string_attr(service_attr) if service_attr is not None else DEFAULT_SERVICE
        version = _string_attr(resource.get("service.version"))
        environment = _string_attr(resource.get("deployment.environment.name"))
        if environment is None:
            environment = _string_attr(resource.get("deployment.environment"))
        if service is None or not _SERVICE_RE.fullmatch(service):
            out.rejections["invalid resource service.name"] += n_spans
            continue
        if version is not None and not _VERSION_RE.fullmatch(version):
            out.rejections["invalid resource service.version"] += n_spans
            continue
        if environment is not None and not _ENVIRONMENT_RE.fullmatch(environment):
            out.rejections["invalid resource deployment.environment"] += n_spans
            continue
        bucket = out.by_service.setdefault(service, [])
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                reason = _reject_reason(span, earliest, latest)
                if reason is not None:
                    out.rejections[reason] += 1
                    continue
                bucket.append(encode_span(_wire(span, version, environment)))
                out.accepted += 1
        if not bucket:
            del out.by_service[service]
    return out


def _reject_reason(span: Any, earliest: int, latest: int) -> str | None:
    trace_id, span_id, parent = span.trace_id, span.span_id, span.parent_span_id
    if len(trace_id) != 16 or trace_id == _ZERO16:
        return "invalid trace_id"
    if len(span_id) != 8 or span_id == _ZERO8:
        return "invalid span_id"
    if parent and (len(parent) != 8 or parent in (_ZERO8, span_id)):
        return "invalid parent_span_id"
    start, end = span.start_time_unix_nano, span.end_time_unix_nano
    if start == 0 or start > _MAX_INT64 or end > _MAX_INT64:
        return "invalid start/end time"
    if end < start:
        return "end time before start time"
    if start > latest:
        return "start time too far in the future"
    if start < earliest:
        return "start time older than the accepted age"
    if span.kind not in _KINDS:
        return "unknown span kind"
    return None


def _wire(span: Any, version: str | None, environment: str | None) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    budget = limits.MAX_ATTRIBUTES_CHARS
    http_route: str | None = None
    http_status: int | None = None
    legacy_status: int | None = None
    for kv in span.attributes:
        key = kv.key
        if key == "http.route":
            route = _string_attr(kv.value)
            if route is not None:
                http_route = _clean(route, limits.MAX_HTTP_ROUTE_LEN)
        elif key == "http.response.status_code":
            http_status = _status_code(kv.value)
        elif key == "http.status_code":
            legacy_status = _status_code(kv.value)
        if len(attributes) >= limits.MAX_ATTRIBUTES or not key:
            continue
        if len(key) > limits.MAX_ATTRIBUTE_KEY_LEN or key in attributes:
            continue
        value = _attribute(kv.value)
        if value is None:
            continue
        cost = len(key) + (
            sum(len(str(v)) for v in value) if isinstance(value, list) else len(str(value))
        )
        if cost > budget:
            continue
        budget -= cost
        attributes[_clean(key, limits.MAX_ATTRIBUTE_KEY_LEN)] = value
    parent = span.parent_span_id
    return {
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_span_id": parent.hex() if parent else None,
        "service_version": version,
        "environment": environment,
        "name": _clean(span.name, limits.MAX_SPAN_NAME_LEN),
        "kind": _KINDS[span.kind],
        "start_ns": int(span.start_time_unix_nano),
        "end_ns": int(span.end_time_unix_nano),
        "status_code": _STATUS.get(span.status.code, "UNSET"),
        "http_route": http_route,
        "http_status": http_status if http_status is not None else legacy_status,
        "attributes": attributes,
    }
