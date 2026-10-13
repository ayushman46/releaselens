"""POST /v1/traces against real Postgres (API keys) and Kafka (telemetry.spans)."""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from confluent_kafka import OFFSET_BEGINNING, Consumer, Message, TopicPartition
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from rl_api.apikeys import create_api_key
from rl_api.app import create_app
from rl_api.settings import Settings
from rl_api.testing import json_body, protobuf_body, to_otlp
from rl_core.events import TELEMETRY_SPANS
from rl_core.events.spans import SpanBatchEnvelope, parse_span_batch
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind, StatusCode
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization

pytestmark = pytest.mark.integration

PROTOBUF = {"Content-Type": "application/x-protobuf"}
JSON = {"Content-Type": "application/json"}


class Keys:
    def __init__(self, tenant: uuid.UUID, writer: str, reader: str) -> None:
        self.tenant, self.writer, self.reader = tenant, writer, reader

    def auth(self, key: str | None = None, **extra: str) -> dict[str, str]:
        return {"x-releaselens-key": key or self.writer, **extra}


def _tenant(url: str) -> Keys:
    async def go() -> Keys:
        engine = create_engine(url, pool_size=1)
        try:
            async with create_session_factory(engine).begin() as s:
                org = await create_organization(s, name="T", slug=f"t-{uuid.uuid4().hex[:12]}")
                writer = await create_api_key(s, org.id, "otel", ["telemetry:write"])
                reader = await create_api_key(s, org.id, "ro", ["read"])
            return Keys(org.id, writer.plaintext, reader.plaintext)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture(scope="module")
def settings(api_db_url: str, kafka_bootstrap: str) -> Settings:
    return Settings(
        env="test", database_url=api_db_url, kafka_bootstrap=kafka_bootstrap, otel_enabled=False
    )


@pytest.fixture(scope="module")
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def keys(api_db_url: str) -> Keys:
    return _tenant(api_db_url)


def _span(i: int, service: str = "payment", **kw: Any) -> Span:
    start = time.time_ns() - 30 * 10**9 + i
    fields: dict[str, Any] = {
        "tenant_id": "x",
        "trace_id": f"{i + 1:032x}",
        "span_id": f"{i + 1:016x}",
        "parent_span_id": None,
        "service": service,
        "service_version": "v1",
        "environment": "demo",
        "name": "/x",
        "kind": SpanKind.SERVER,
        "start_ns": start,
        "end_ns": start + 5 * NS_PER_MS,
        "status_code": StatusCode.UNSET,
        "http_route": "/x",
        "http_status": 200,
    }
    fields.update(kw)
    return Span(**fields)


def _messages(bootstrap: str, tenant: uuid.UUID, expect: int) -> list[Message]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"t-{uuid.uuid4()}",
            "enable.auto.commit": False,
        }
    )
    try:
        md = consumer.list_topics(TELEMETRY_SPANS, timeout=10)
        consumer.assign(
            [
                TopicPartition(TELEMETRY_SPANS, p, OFFSET_BEGINNING)
                for p in md.topics[TELEMETRY_SPANS].partitions
            ]
        )
        found: list[Message] = []
        deadline = time.monotonic() + 15
        prefix = f"{tenant}:".encode()
        while time.monotonic() < deadline and len(found) < expect:
            msg = consumer.poll(0.3)
            if msg is not None and msg.error() is None and (msg.key() or b"").startswith(prefix):
                found.append(msg)
        linger = time.monotonic() + 0.5
        while time.monotonic() < linger:
            msg = consumer.poll(0.1)
            if msg is not None and msg.error() is None and (msg.key() or b"").startswith(prefix):
                found.append(msg)
        return found
    finally:
        consumer.close()


def _envelopes(bootstrap: str, keys: Keys, expect: int) -> list[SpanBatchEnvelope]:
    msgs = _messages(bootstrap, keys.tenant, expect)
    assert len(msgs) == expect
    for m in msgs:
        headers = dict(m.headers() or [])
        assert headers["rl-event-type"] == b"telemetry.span_batch"
    envelopes = [parse_span_batch(m.value()) for m in msgs]
    for m, env in zip(msgs, envelopes, strict=True):
        assert m.key() == f"{keys.tenant}:{env.service}".encode()
    return envelopes


# ----------------------------------------------------------------------------- auth
def test_missing_key_is_401_and_wrong_scope_is_403(client: TestClient, keys: Keys) -> None:
    body = protobuf_body([_span(0)])
    resp = client.post("/v1/traces", content=body, headers=PROTOBUF)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    resp = client.post("/v1/traces", content=body, headers={**PROTOBUF, **keys.auth(keys.reader)})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "insufficient_scope"
    resp = client.post(
        "/v1/traces",
        content=body,
        headers={**PROTOBUF, "Authorization": f"Bearer {keys.writer}"},
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------------------- happy paths
def test_protobuf_spans_land_on_kafka_keyed_by_tenant_and_service(
    client: TestClient, keys: Keys, kafka_bootstrap: str
) -> None:
    other_tenant = str(uuid.uuid4())
    spans = [_span(i, "payment") for i in range(5)] + [_span(i, "fraud") for i in range(5, 8)]
    body = protobuf_body(
        spans, extra_resource={"tenant_id": other_tenant, "releaselens.tenant_id": other_tenant}
    )
    resp = client.post("/v1/traces", content=body, headers={**PROTOBUF, **keys.auth()})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/x-protobuf"
    parsed = ExportTraceServiceResponse.FromString(resp.content)
    assert not parsed.HasField("partial_success")

    envelopes = _envelopes(kafka_bootstrap, keys, expect=2)  # one message per service
    by_service = {e.service: e for e in envelopes}
    assert set(by_service) == {"payment", "fraud"}
    assert all(e.tenant_id == keys.tenant for e in envelopes)  # never the spoofed resource value
    assert [s.span_id for s in by_service["payment"].spans] == [s.span_id for s in spans[:5]]
    assert len(by_service["fraud"].spans) == 3
    assert other_tenant not in json.dumps([e.model_dump(mode="json") for e in envelopes])


def test_json_and_gzip_bodies(client: TestClient, keys: Keys, kafka_bootstrap: str) -> None:
    resp = client.post(
        "/v1/traces",
        content=json_body([_span(1, "orders")]),
        headers={"Content-Type": "application/json; charset=utf-8", **keys.auth()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {}

    for body, ctype in (
        (protobuf_body([_span(2, "orders")], compress=True), PROTOBUF),
        (json_body([_span(3, "orders")], compress=True), JSON),
    ):
        resp = client.post(
            "/v1/traces",
            content=body,
            headers={**ctype, **keys.auth(**{"Content-Encoding": "gzip"})},
        )
        assert resp.status_code == 200, resp.text
    spans = [s for e in _envelopes(kafka_bootstrap, keys, expect=3) for s in e.spans]
    assert sorted(s.span_id for s in spans) == [f"{i + 1:016x}" for i in (1, 2, 3)]


def test_partial_success_reports_rejected_spans(
    client: TestClient, keys: Keys, kafka_bootstrap: str
) -> None:
    request = to_otlp([_span(1), _span(2), _span(3)])
    bad = request.resource_spans[0].scope_spans[0].spans
    bad[0].end_time_unix_nano = bad[0].start_time_unix_nano - 1
    bad[1].start_time_unix_nano = time.time_ns() + 3 * 86_400 * 10**9
    bad[1].end_time_unix_nano = bad[1].start_time_unix_nano + 1
    resp = client.post(
        "/v1/traces", content=request.SerializeToString(), headers={**PROTOBUF, **keys.auth()}
    )
    assert resp.status_code == 200
    partial = ExportTraceServiceResponse.FromString(resp.content).partial_success
    assert partial.rejected_spans == 2
    assert "end time before start time" in partial.error_message
    assert "future" in partial.error_message
    [env] = _envelopes(kafka_bootstrap, keys, expect=1)
    assert [s.span_id for s in env.spans] == [f"{4:016x}"]  # _span(3)

    # Every span rejected: still 200 + partial_success (the data is bad, retrying cannot help).
    resp = client.post(
        "/v1/traces",
        content=json.dumps(
            {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "00", "spanId": "01"}]}]}]}
        ),
        headers={**JSON, **keys.auth()},
    )
    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedSpans"] == "1"


# ----------------------------------------------------------------------------- refusals
@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
        ({}, 415, "unsupported_media_type"),
        ({**PROTOBUF, "Content-Encoding": "br"}, 415, "unsupported_media_type"),
    ],
)
def test_unsupported_media(
    client: TestClient, keys: Keys, headers: dict[str, str], status: int, code: str
) -> None:
    resp = client.post("/v1/traces", content=b"x", headers={**headers, **keys.auth()})
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


def test_undecodable_bodies_are_400(client: TestClient, keys: Keys) -> None:
    for body, headers in (
        (b"\x0a\xff\xff", PROTOBUF),
        (b"{nope", JSON),
        (b"not gzip", {**PROTOBUF, "Content-Encoding": "gzip"}),
    ):
        resp = client.post("/v1/traces", content=body, headers={**headers, **keys.auth()})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_otlp"


def test_oversized_bodies_and_gzip_bombs_are_413(client: TestClient, keys: Keys) -> None:
    bomb = gzip.compress(bytes(17 * 1024 * 1024), compresslevel=9)  # inflates past 16 MiB
    assert len(bomb) < 64 * 1024
    resp = client.post(
        "/v1/traces", content=bomb, headers={**PROTOBUF, "Content-Encoding": "gzip", **keys.auth()}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"

    big = bytes(8 * 1024 * 1024 + 1)
    resp = client.post("/v1/traces", content=big, headers={**PROTOBUF, **keys.auth()})
    assert resp.status_code == 413


def test_kafka_down_is_503_with_retry_after(settings: Settings, keys: Keys) -> None:
    dead = settings.model_copy(
        update={"kafka_bootstrap": "127.0.0.1:1", "kafka_delivery_timeout_s": 1.0}
    )
    with TestClient(create_app(dead)) as c:
        started = time.perf_counter()
        resp = c.post(
            "/v1/traces", content=protobuf_body([_span(1)]), headers={**PROTOBUF, **keys.auth()}
        )
        elapsed = time.perf_counter() - started
    assert resp.status_code == 503, resp.text
    assert resp.headers["retry-after"] == "5"
    assert resp.json()["error"]["code"] == "event_bus_unavailable"
    assert elapsed < 5.0
