"""Test helpers: build OTLP trace requests from rl_core spans (not imported by production code).

Spans are grouped into one ``ResourceSpans`` per ``(service, version, environment)``, the shape
an OpenTelemetry SDK + Collector produce.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Iterable, Mapping
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Status
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

from rl_core.telemetry.model import Span, SpanKind, StatusCode

_KIND = {
    SpanKind.INTERNAL: OtlpSpan.SPAN_KIND_INTERNAL,
    SpanKind.SERVER: OtlpSpan.SPAN_KIND_SERVER,
    SpanKind.CLIENT: OtlpSpan.SPAN_KIND_CLIENT,
    SpanKind.PRODUCER: OtlpSpan.SPAN_KIND_PRODUCER,
    SpanKind.CONSUMER: OtlpSpan.SPAN_KIND_CONSUMER,
}
_STATUS = {
    StatusCode.UNSET: Status.STATUS_CODE_UNSET,
    StatusCode.OK: Status.STATUS_CODE_OK,
    StatusCode.ERROR: Status.STATUS_CODE_ERROR,
}


def any_value(value: Any) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, list | tuple):
        out = AnyValue()
        out.array_value.values.extend(any_value(v) for v in value)
        return out
    return AnyValue(string_value=str(value))


def key_values(attrs: Mapping[str, Any]) -> list[KeyValue]:
    return [KeyValue(key=k, value=any_value(v)) for k, v in attrs.items()]


def to_otlp(
    spans: Iterable[Span], *, extra_resource: Mapping[str, Any] | None = None
) -> ExportTraceServiceRequest:
    groups: dict[tuple[str, str | None, str | None], list[Span]] = {}
    for span in spans:
        groups.setdefault((span.service, span.service_version, span.environment), []).append(span)
    request = ExportTraceServiceRequest()
    for (service, version, environment), members in groups.items():
        resource: dict[str, Any] = {"service.name": service}
        if version is not None:
            resource["service.version"] = version
        if environment is not None:
            resource["deployment.environment"] = environment
        resource.update(extra_resource or {})
        rs = ResourceSpans()
        rs.resource.attributes.extend(key_values(resource))
        scope = rs.scope_spans.add()
        scope.scope.name = "rl-tests"
        for span in members:
            attrs: dict[str, Any] = dict(span.attributes)
            if span.http_route is not None:
                attrs["http.route"] = span.http_route
            if span.http_status is not None:
                attrs["http.response.status_code"] = span.http_status
            out = scope.spans.add()
            out.trace_id = bytes.fromhex(span.trace_id)
            out.span_id = bytes.fromhex(span.span_id)
            if span.parent_span_id is not None:
                out.parent_span_id = bytes.fromhex(span.parent_span_id)
            out.name = span.name
            out.kind = _KIND[span.kind]
            out.start_time_unix_nano = span.start_ns
            out.end_time_unix_nano = span.end_ns
            out.status.code = _STATUS[span.status_code]
            out.attributes.extend(key_values(attrs))
        request.resource_spans.append(rs)
    return request


def to_otlp_json(request: ExportTraceServiceRequest) -> dict[str, Any]:
    """OTLP/JSON: protobuf JSON mapping with hex trace/span ids and integer enums."""
    from google.protobuf import json_format

    obj: dict[str, Any] = json_format.MessageToDict(request, use_integers_for_enums=True)
    for rs in obj.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for name in ("traceId", "spanId", "parentSpanId"):
                    if name in span:
                        span[name] = base64.b64decode(span[name]).hex()
    return obj


def protobuf_body(spans: Iterable[Span], *, compress: bool = False, **kw: Any) -> bytes:
    data = to_otlp(spans, **kw).SerializeToString()
    return gzip.compress(data) if compress else data


def json_body(spans: Iterable[Span], *, compress: bool = False, **kw: Any) -> bytes:
    data = json.dumps(to_otlp_json(to_otlp(spans, **kw))).encode()
    return gzip.compress(data) if compress else data
