"""``telemetry.spans`` envelope: encoding, chunking, strict parsing."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from rl_core.events.errors import (
    EventTooLargeError,
    EventValidationError,
    MalformedEventError,
    UnknownEventTypeError,
    UnknownSchemaVersionError,
)
from rl_core.events.spans import (
    MAX_SPAN_BATCH_BYTES,
    encode_span,
    encode_span_batches,
    is_valid_span_id,
    is_valid_trace_id,
    parse_span_batch,
)
from rl_core.events.topics import TELEMETRY_SPANS_DLQ, span_key
from rl_core.telemetry.model import SpanKind, StatusCode

TENANT = uuid.UUID("01920000-0000-7000-8000-000000000001")
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _span(i: int = 1, **overrides: Any) -> dict[str, Any]:
    span: dict[str, Any] = {
        "trace_id": f"{i:032x}",
        "span_id": f"{i:016x}",
        "parent_span_id": None,
        "service_version": "v2",
        "environment": "demo",
        "name": "/charge",
        "kind": "SERVER",
        "start_ns": 1_760_356_800_000_000_000 + i,
        "end_ns": 1_760_356_800_061_000_000 + i,
        "status_code": "ERROR",
        "http_route": "/charge",
        "http_status": 500,
        "attributes": {"http.method": "POST", "retries": 2, "ok": False, "tags": ["a", "b"]},
    }
    span.update(overrides)
    return span


def _encode(spans: list[dict[str, Any]], **kw: Any) -> list[bytes]:
    return list(
        encode_span_batches(
            tenant_id=TENANT,
            service="payment",
            spans=[encode_span(s) for s in spans],
            received_at=NOW,
            **kw,
        )
    )


def test_round_trip_to_rl_core_spans() -> None:
    [raw] = _encode([_span(1), _span(2, parent_span_id=f"{1:016x}", kind="CLIENT")])
    env = parse_span_batch(raw)
    assert env.tenant_id == TENANT
    assert env.service == "payment"
    spans = env.to_spans()
    assert [s.kind for s in spans] == [SpanKind.SERVER, SpanKind.CLIENT]
    first = spans[0]
    assert first.tenant_id == str(TENANT)
    assert first.service == "payment"
    assert first.status_code is StatusCode.ERROR
    assert first.is_error
    assert first.duration_ms == pytest.approx(61.0)
    assert dict(first.attributes) == {
        "http.method": "POST",
        "retries": 2,
        "ok": False,
        "tags": ["a", "b"],
    }
    assert spans[1].parent_key == (f"{2:032x}", f"{1:016x}")


def test_large_batches_are_split_into_messages_that_each_fit() -> None:
    spans = [_span(i, attributes={"blob": "x" * 200}) for i in range(1, 2001)]
    messages = _encode(spans, max_bytes=64 * 1024)
    assert len(messages) > 1
    assert all(len(m) <= 64 * 1024 for m in messages)
    parsed = [parse_span_batch(m) for m in messages]
    assert sum(len(p.spans) for p in parsed) == 2000
    assert len({p.batch_id for p in parsed}) == len(parsed)  # one batch id per message
    assert [s.span_id for p in parsed for s in p.spans] == [f"{i:016x}" for i in range(1, 2001)]


def test_a_span_that_cannot_fit_any_message_is_refused() -> None:
    with pytest.raises(EventTooLargeError):
        _encode([_span(1, attributes={"blob": "x" * 5000})], max_bytes=4096)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (None, MalformedEventError),
        (b"\xff\xfe", MalformedEventError),
        (b"not json", MalformedEventError),
        (b"[1, 2]", MalformedEventError),
        (b'{"type": "deployment.completed", "schema_version": 1}', UnknownEventTypeError),
        (b'{"type": "telemetry.span_batch", "schema_version": 2}', UnknownSchemaVersionError),
        (b'{"type": "telemetry.span_batch", "schema_version": true}', UnknownSchemaVersionError),
        (b"x" * (MAX_SPAN_BATCH_BYTES + 1), EventTooLargeError),
    ],
)
def test_parse_classifies_bad_messages(raw: bytes | None, error: type[Exception]) -> None:
    with pytest.raises(error):
        parse_span_batch(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_ns": 1},  # ends before it starts
        {"trace_id": "0" * 32},
        {"span_id": "0" * 16},
        {"trace_id": "ABC"},
        {"parent_span_id": f"{1:016x}"},  # its own parent
        {"kind": "WEIRD"},
        {"http_status": 99},
        {"environment": "has space"},
        {"unknown_field": 1},
    ],
)
def test_parse_rejects_invalid_spans(overrides: dict[str, Any]) -> None:
    [raw] = _encode([_span(1, **overrides)])
    with pytest.raises(EventValidationError) as info:
        parse_span_batch(raw)
    # The error summary never echoes the rejected values.
    assert "has space" not in str(info.value.errors)


def test_parse_rejects_extra_envelope_fields_and_empty_batches() -> None:
    [raw] = _encode([_span(1)])
    obj = json.loads(raw)
    obj["extra"] = 1
    with pytest.raises(EventValidationError):
        parse_span_batch(json.dumps(obj).encode())
    obj.pop("extra")
    obj["spans"] = []
    with pytest.raises(EventValidationError):
        parse_span_batch(json.dumps(obj).encode())


def test_ids_and_keys() -> None:
    assert is_valid_trace_id("a" * 32)
    assert not is_valid_trace_id("0" * 32)
    assert not is_valid_trace_id("A" * 32)
    assert is_valid_span_id("0123456789abcdef")
    assert not is_valid_span_id("0" * 16)
    assert span_key(TENANT, "payment") == f"{TENANT}:payment"
    assert TELEMETRY_SPANS_DLQ == "telemetry.spans.dlq"
