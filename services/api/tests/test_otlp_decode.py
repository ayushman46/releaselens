"""OTLP decoding and span mapping (no network, no Docker)."""

from __future__ import annotations

import gzip
import json
import math
import time
import zlib
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from rl_api.otlp import (
    ConvertedRequest,
    OtlpDecodeError,
    PayloadTooLargeError,
    TimeBounds,
    convert_request,
    decode_request,
    encode_response,
    gunzip_bounded,
)
from rl_api.testing import any_value, to_otlp, to_otlp_json
from rl_core.events.spans import encode_span_batches, parse_span_batch
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind, StatusCode

NOW_NS = time.time_ns()
DAY_NS = 86_400 * 10**9
BOUNDS = TimeBounds(now_ns=NOW_NS, max_future_ns=DAY_NS, max_age_ns=7 * DAY_NS)


def _span(i: int = 1, **kw: Any) -> Span:
    fields: dict[str, Any] = {
        "tenant_id": "ignored-by-the-encoder",
        "trace_id": f"{i:032x}",
        "span_id": f"{i:016x}",
        "parent_span_id": None,
        "service": "payment",
        "service_version": "v2.18",
        "environment": "demo",
        "name": "/charge",
        "kind": SpanKind.SERVER,
        "start_ns": NOW_NS - 60 * 10**9,
        "end_ns": NOW_NS - 60 * 10**9 + 61 * NS_PER_MS,
        "status_code": StatusCode.ERROR,
        "http_route": "/charge",
        "http_status": 503,
    }
    fields.update(kw)
    return Span(**fields)


def _wire(converted: ConvertedRequest, service: str = "payment") -> list[dict[str, Any]]:
    return [json.loads(b) for b in converted.by_service[service]]


def test_protobuf_maps_every_field() -> None:
    spans = [_span(1), _span(2, parent_span_id=f"{1:016x}", kind=SpanKind.CLIENT, http_status=None)]
    request = decode_request(to_otlp(spans).SerializeToString(), "protobuf")
    out = convert_request(request, BOUNDS)
    assert (out.accepted, out.rejected) == (2, 0)
    first, second = _wire(out)
    assert first == {
        "trace_id": f"{1:032x}",
        "span_id": f"{1:016x}",
        "parent_span_id": None,
        "service_version": "v2.18",
        "environment": "demo",
        "name": "/charge",
        "kind": "SERVER",
        "start_ns": spans[0].start_ns,
        "end_ns": spans[0].end_ns,
        "status_code": "ERROR",
        "http_route": "/charge",
        "http_status": 503,
        "attributes": {"http.route": "/charge", "http.response.status_code": 503},
    }
    assert (second["kind"], second["parent_span_id"]) == ("CLIENT", f"{1:016x}")
    # Encoded wire spans form valid envelopes.
    import uuid
    from datetime import UTC, datetime

    [raw] = encode_span_batches(
        tenant_id=uuid.uuid4(), service="payment", spans=out.by_service["payment"],
        received_at=datetime.now(UTC),
    )  # fmt: skip
    assert [s.span_id for s in parse_span_batch(raw).to_spans()] == [f"{1:016x}", f"{2:016x}"]


def test_json_with_hex_ids_string_ints_and_either_field_case() -> None:
    obj = to_otlp_json(to_otlp([_span(7)]))
    span = obj["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == f"{7:032x}"  # hex, per OTLP/JSON
    assert isinstance(span["startTimeUnixNano"], str)  # 64-bit ints as strings
    out = convert_request(decode_request(json.dumps(obj).encode(), "json"), BOUNDS)
    assert _wire(out)[0]["trace_id"] == f"{7:032x}"

    snake = {
        "resource_spans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "fraud"}}]
                },
                "scope_spans": [
                    {
                        "spans": [
                            {
                                "trace_id": "ab" * 16,
                                "span_id": "cd" * 8,
                                "name": "score",
                                "kind": 2,
                                "start_time_unix_nano": NOW_NS,
                                "end_time_unix_nano": str(NOW_NS + 5),
                                "status": {"code": 2},
                                "links": [{"traceId": "zz", "spanId": "zz"}],
                                "unknownField": True,
                            }
                        ]
                    }
                ],
            }
        ]
    }
    out = convert_request(decode_request(json.dumps(snake).encode(), "json"), BOUNDS)
    [wire] = _wire(out, "fraud")
    assert (wire["trace_id"], wire["kind"], wire["status_code"]) == ("ab" * 16, "SERVER", "ERROR")
    assert wire["environment"] is None


def test_invalid_hex_ids_reject_the_span_not_the_request() -> None:
    obj = to_otlp_json(to_otlp([_span(1), _span(2)]))
    obj["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "not-hex"
    out = convert_request(decode_request(json.dumps(obj).encode(), "json"), BOUNDS)
    assert (out.accepted, out.rejected) == (1, 1)
    assert out.error_message() == "1 span(s): invalid trace_id"


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        (b"\x0a\xff\xff\xff", "protobuf"),
        (b"{not json", "json"),
        (b"[1, 2]", "json"),
        (b'{"resourceSpans": "nope"}', "json"),
        (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": "x"}]}]}]}', "json"),
    ],
)
def test_undecodable_bodies(body: bytes, encoding: Any) -> None:
    with pytest.raises(OtlpDecodeError):
        decode_request(body, encoding)


def test_unknown_enum_names_are_tolerated_and_bad_spans_rejected() -> None:
    body = b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"kind": "WHAT"}]}]}]}'
    out = convert_request(decode_request(body, "json"), BOUNDS)
    assert (out.accepted, dict(out.rejections)) == (0, {"invalid trace_id": 1})


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"trace_id": bytes(16)}, "invalid trace_id"),
        ({"trace_id": b"\x01" * 8}, "invalid trace_id"),
        ({"span_id": bytes(8)}, "invalid span_id"),
        ({"parent_span_id": b"\x00\x00\x00\x00\x00\x00\x00\x01"}, "invalid parent_span_id"),
        ({"parent_span_id": b"\x01\x02"}, "invalid parent_span_id"),
        ({"end_time_unix_nano": 1}, "end time before start time"),
        ({"start_time_unix_nano": 0, "end_time_unix_nano": 0}, "invalid start/end time"),
        ({"start_time_unix_nano": NOW_NS + 2 * DAY_NS, "end_time_unix_nano": NOW_NS + 3 * DAY_NS},
         "start time too far in the future"),
        ({"start_time_unix_nano": NOW_NS - 8 * DAY_NS}, "start time older than the accepted age"),
        ({"kind": 9}, "unknown span kind"),
    ],
)  # fmt: skip
def test_span_rejections(changes: dict[str, Any], reason: str) -> None:
    request = to_otlp([_span(1, span_id=f"{1:016x}"), _span(2)])
    span = request.resource_spans[0].scope_spans[0].spans[0]
    for name, value in changes.items():
        setattr(span, name, value)
    out = convert_request(request, BOUNDS)
    assert (out.accepted, out.rejected) == (1, 1)
    assert dict(out.rejections) == {reason: 1}


@pytest.mark.parametrize(
    ("resource", "reason"),
    [
        ({"service.name": "has space"}, "invalid resource service.name"),
        ({"service.name": 42}, "invalid resource service.name"),
        ({"service.version": "v 1"}, "invalid resource service.version"),
        ({"deployment.environment": "-bad"}, "invalid resource deployment.environment"),
    ],
)
def test_invalid_resources_reject_all_their_spans(resource: dict[str, Any], reason: str) -> None:
    request = to_otlp([_span(1), _span(2)])
    attrs = request.resource_spans[0].resource.attributes
    for key, value in resource.items():
        for kv in attrs:
            if kv.key == key:
                kv.value.CopyFrom(any_value(value))
                break
        else:
            attrs.append(KeyValue(key=key, value=any_value(value)))
    out = convert_request(request, BOUNDS)
    assert (out.accepted, dict(out.rejections)) == (0, {reason: 2})
    assert out.by_service == {}


def test_resource_defaults_and_environment_precedence() -> None:
    request = to_otlp([_span(1, environment=None)], extra_resource={})
    attrs = request.resource_spans[0].resource.attributes
    del attrs[:]
    attrs.extend(
        [
            KeyValue(key="deployment.environment", value=any_value("old")),
            KeyValue(key="deployment.environment.name", value=any_value("new")),
            KeyValue(key="tenant_id", value=any_value("someone-else")),
        ]
    )
    out = convert_request(request, BOUNDS)
    [wire] = _wire(out, "unknown_service")
    assert (wire["environment"], wire["service_version"]) == ("new", None)
    assert "tenant_id" not in json.dumps(wire)  # resource attributes are never copied


def test_attributes_are_bounded() -> None:
    request = to_otlp([_span(1, http_route=None, http_status=None)])
    span = request.resource_spans[0].scope_spans[0].spans[0]
    nested = AnyValue()
    nested.kvlist_value.values.append(KeyValue(key="x", value=any_value(1)))
    span.attributes.extend(
        [
            KeyValue(key="long.string", value=any_value("é" * 1000 + "\x00")),
            KeyValue(key="k" * 129, value=any_value("dropped: key too long")),
            KeyValue(key="nested", value=nested),
            KeyValue(key="nan", value=AnyValue(double_value=math.nan)),
            KeyValue(key="bytes", value=AnyValue(bytes_value=b"\x00")),
            KeyValue(key="array", value=any_value(list(range(40)))),
            KeyValue(key="nul\x00key", value=any_value("a\x00b")),
            KeyValue(key="http.status_code", value=any_value("404")),
        ]
    )
    span.attributes.extend(KeyValue(key=f"a{i}", value=any_value(i)) for i in range(100))
    [wire] = _wire(convert_request(request, BOUNDS))
    attrs = wire["attributes"]
    assert len(attrs) == 64
    assert attrs["long.string"] == "é" * 256
    assert "k" * 129 not in attrs
    assert {"nested", "nan", "bytes"}.isdisjoint(attrs)
    assert attrs["array"] == list(range(16))
    assert attrs["nul�key"] == "a�b"
    assert wire["http_status"] == 404  # legacy attribute honoured
    assert "a63" not in attrs  # stops at 64


def test_attribute_budget_caps_total_size() -> None:
    request = to_otlp([_span(1)])
    span = request.resource_spans[0].scope_spans[0].spans[0]
    span.attributes.extend(
        KeyValue(key=f"key-{i:03d}-padding", value=any_value("x" * 256)) for i in range(63)
    )
    [wire] = _wire(convert_request(request, BOUNDS))
    size = sum(len(k) + len(str(v)) for k, v in wire["attributes"].items())
    assert size <= 16 * 1024
    assert len(wire["attributes"]) < 64


def test_response_encodings() -> None:
    raw, media = encode_response(0, "", "protobuf")
    assert (raw, media) == (b"", "application/x-protobuf")
    raw, media = encode_response(3, "3 span(s): invalid trace_id", "protobuf")
    parsed = ExportTraceServiceResponse.FromString(raw)
    assert parsed.partial_success.rejected_spans == 3
    raw, media = encode_response(3, "why", "json")
    assert media == "application/json"
    assert json.loads(raw) == {"partialSuccess": {"rejectedSpans": "3", "errorMessage": "why"}}
    assert json.loads(encode_response(0, "", "json")[0]) == {}


def test_gunzip_bounded() -> None:
    data = b"x" * 10_000
    assert gunzip_bounded(gzip.compress(data), 10_000) == data
    assert gunzip_bounded(gzip.compress(b"ab") + gzip.compress(b"cd"), 100) == b"abcd"
    with pytest.raises(PayloadTooLargeError):
        gunzip_bounded(gzip.compress(data), 9_999)
    with pytest.raises(OtlpDecodeError):
        gunzip_bounded(gzip.compress(data)[:-12], 100_000)
    with pytest.raises(OtlpDecodeError):
        gunzip_bounded(b"not gzip at all", 100)


def test_gzip_bomb_is_refused_without_inflating_it() -> None:
    bomb = gzip.compress(bytes(512 * 1024 * 1024), compresslevel=9)  # 512 MiB of zeros
    assert len(bomb) < 1024 * 1024
    started = time.perf_counter()
    with pytest.raises(PayloadTooLargeError):
        gunzip_bounded(bomb, 16 * 1024 * 1024)
    assert time.perf_counter() - started < 2.0  # stops at the limit
    assert zlib.MAX_WBITS == 15
