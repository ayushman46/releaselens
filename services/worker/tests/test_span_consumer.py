"""SpanConsumer against real Kafka + Postgres: idempotency, edge exactness, partitions, DLQ,
database outages and measured throughput."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import tracegen as tg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import dlq
from rl_core.telemetry.model import NS_PER_MS, Span, SpanKind, StatusCode, datetime_to_ns
from rl_db import create_engine, create_session_factory
from rl_db.admin.partitions import ensure_partitions_for_days, list_partitions
from rl_db.models import ServiceEdge, ServiceMinuteRollup, SpanRecord
from rl_worker.consumers.spans import SpanConsumer
from rl_worker.kafka import AsyncProducer
from rl_worker.maintenance import TelemetryMaintenance
from rl_worker.retry import Backoff
from rl_worker.testing import (
    TcpProxy,
    committed_vs_end,
    headers_of,
    produce_many,
    read_topic,
    span_messages,
    wait_until,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
FAST = Backoff(base_s=0.05, cap_s=0.5, max_attempts=3)


def measure(name: str, value: float, unit: str = "") -> None:
    print(f"[measure] {name}={value:.1f}{unit}", file=sys.stderr)


@contextlib.asynccontextmanager
async def running(consumer: SpanConsumer) -> AsyncIterator[asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        yield task
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


@pytest.fixture
def build(
    kafka_bootstrap: str, sessions: Sessions, producer: AsyncProducer, span_topic: str
) -> Callable[..., SpanConsumer]:
    group = f"rl-spans-{uuid.uuid4().hex[:8]}"

    def make(**kw: Any) -> SpanConsumer:
        args: dict[str, Any] = {
            "bootstrap_servers": kafka_bootstrap,
            "sessions": sessions,
            "producer": producer,
            "client_id": f"test-{uuid.uuid4().hex[:6]}",
            "group_id": group,
            "topic": span_topic,
            "backoff": FAST,
            "poll_timeout_s": 0.2,
            "consumer_overrides": {"session.timeout.ms": 6000, "heartbeat.interval.ms": 1000},
        }
        args.update(kw)
        return SpanConsumer(**args)

    make.group = group  # type: ignore[attr-defined]
    return make


def _chain(tenant: uuid.UUID, trace: int, start_ns: int, services: list[str]) -> list[Span]:
    """services[0] → services[1] → …: a SERVER span per service, a CLIENT span per call."""
    spans: list[Span] = []
    parent: str | None = None
    sid = 0
    for depth, service in enumerate(services):
        sid += 1
        server = f"{trace:08x}{sid:08x}"
        t0 = start_ns + depth * NS_PER_MS
        spans.append(
            Span(
                tenant_id=str(tenant),
                trace_id=f"{trace:032x}",
                span_id=server,
                parent_span_id=parent,
                service=service,
                service_version="v1",
                environment="demo",
                name=service,
                kind=SpanKind.SERVER,
                start_ns=t0,
                end_ns=t0 + (20 - depth) * NS_PER_MS,
                status_code=StatusCode.UNSET,
                http_status=200,
            )
        )
        if depth + 1 < len(services):
            sid += 1
            client = f"{trace:08x}{sid:08x}"
            spans.append(
                Span(
                    tenant_id=str(tenant),
                    trace_id=f"{trace:032x}",
                    span_id=client,
                    parent_span_id=server,
                    service=service,
                    service_version="v1",
                    environment="demo",
                    name=f"{service} call",
                    kind=SpanKind.CLIENT,
                    start_ns=t0 + NS_PER_MS // 2,
                    end_ns=t0 + (19 - depth) * NS_PER_MS,
                )
            )
            parent = client
    return spans


async def _state(
    sessions: Sessions, tenant: uuid.UUID
) -> tuple[int, dict[str, int], tuple[int, int, int]]:
    async with sessions() as s:
        spans = int(
            await s.scalar(
                select(func.count()).select_from(SpanRecord).where(SpanRecord.tenant_id == tenant)
            )
            or 0
        )
        edges = {
            f"{r.caller_service}->{r.callee_service}": int(r.call_count)
            for r in await s.scalars(select(ServiceEdge).where(ServiceEdge.tenant_id == tenant))
        }
        roll = (
            await s.execute(
                select(
                    func.coalesce(func.sum(ServiceMinuteRollup.request_count), 0),
                    func.coalesce(func.sum(ServiceMinuteRollup.error_count), 0),
                    func.count(),
                ).where(ServiceMinuteRollup.tenant_id == tenant)
            )
        ).one()
    return spans, edges, (int(roll[0]), int(roll[1]), int(roll[2]))


def _now_ns() -> int:
    return datetime_to_ns(datetime.now(UTC) - timedelta(minutes=2))


# ----------------------------------------------------------------------------- idempotency
async def test_redelivered_batches_change_nothing(
    kafka_bootstrap: str,
    span_topic: str,
    build: Callable[..., SpanConsumer],
    sessions: Sessions,
    tenant_id: uuid.UUID,
) -> None:
    start = _now_ns()
    spans = [
        s
        for t in range(1, 41)
        for s in _chain(tenant_id, t, start + t * 10**6, ["gw", "orders", "pay"])
    ]
    messages = span_messages(tenant_id, spans, per_message=7)
    produce_many(kafka_bootstrap, span_topic, messages)
    consumer = build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.messages == len(messages))
    first = await _state(sessions, tenant_id)
    assert first == (200, {"gw->orders": 40, "orders->pay": 40}, (120, 0, first[2][2]))

    produce_many(kafka_bootstrap, span_topic, messages)  # the same bytes again
    again = build()  # same group: continues after the committed offsets
    async with running(again):
        await wait_until(lambda: again.stats.messages == len(messages))
    assert await _state(sessions, tenant_id) == first
    assert (again.stats.spans_received, again.stats.spans_inserted) == (200, 0)
    assert again.stats.edge_calls == 0
    offsets = committed_vs_end(kafka_bootstrap, build.group, span_topic)  # type: ignore[attr-defined]
    assert all(c == end for c, end in offsets.values() if end)


async def test_children_and_parents_in_different_batches_count_each_call_once(
    kafka_bootstrap: str,
    span_topic: str,
    build: Callable[..., SpanConsumer],
    sessions: Sessions,
    tenant_id: uuid.UUID,
) -> None:
    start = _now_ns()
    spans = [s for t in range(1, 31) for s in _chain(tenant_id, t, start, ["gw", "orders", "pay"])]
    callee_side = [s for s in spans if s.service == "pay"]
    middle = [s for s in spans if s.service == "orders"]
    caller_side = [s for s in spans if s.service == "gw"]
    consumer = build()
    async with running(consumer):
        for part in (callee_side, caller_side, middle):  # leaves first, then roots, then the link
            msgs = span_messages(tenant_id, part, per_message=4)
            target = consumer.stats.messages + len(msgs)
            produce_many(kafka_bootstrap, span_topic, msgs)
            await wait_until(lambda target=target: consumer.stats.messages == target)
        # Everything again, interleaved: still nothing new.
        msgs = span_messages(tenant_id, spans, per_message=5)
        target = consumer.stats.messages + len(msgs)
        produce_many(kafka_bootstrap, span_topic, msgs)
        await wait_until(lambda: consumer.stats.messages == target)
    _, edges, _ = await _state(sessions, tenant_id)
    assert edges == {"gw->orders": 30, "orders->pay": 30}


# ----------------------------------------------------------------------------- partitions + DLQ
async def test_partitions_are_created_and_bad_messages_dead_lettered(
    kafka_bootstrap: str,
    span_topic: str,
    build: Callable[..., SpanConsumer],
    sessions: Sessions,
    tenant_id: uuid.UUID,
) -> None:
    old_day = datetime.now(UTC) - timedelta(days=3)
    start = datetime_to_ns(old_day)
    good = span_messages(tenant_id, _chain(tenant_id, 1, start, ["a", "b"]))
    stranger = span_messages(uuid.uuid4(), _chain(tenant_id, 2, start, ["a", "b"]))[:1]
    garbage = [(f"{tenant_id}:x", b"{not json")]
    produce_many(kafka_bootstrap, span_topic, garbage + stranger + good)

    consumer = build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.messages == 1 + len(stranger) + len(good))
    assert consumer.stats.dead_lettered == 2
    async with sessions() as s:
        days = await list_partitions(await s.connection())
    assert old_day.date() in days
    spans, edges, _ = await _state(sessions, tenant_id)
    assert (spans, edges) == (3, {"a->b": 1})

    dead = read_topic(kafka_bootstrap, dlq(span_topic), expect=2)
    types = sorted(headers_of(m)["error_type"].decode() for m in dead)
    assert types == ["malformed_json", "unknown_tenant"]


async def test_maintenance_creates_ahead_and_drops_expired_partitions(sessions: Sessions) -> None:
    old = datetime.now(UTC).date() - timedelta(days=40)
    async with sessions.begin() as s:
        await ensure_partitions_for_days(s, [old])
    maintenance = TelemetryMaintenance(sessions, days_ahead=2, span_retention_days=30)
    result = await maintenance.run_once()
    assert result.dropped >= 1
    async with sessions() as s:
        days = await list_partitions(await s.connection())
    today = datetime.now(UTC).date()
    assert old not in days
    assert {today, today + timedelta(days=1), today + timedelta(days=2)} <= set(days)
    assert (await maintenance.run_once()).created == 0  # idempotent


# ----------------------------------------------------------------------------- outages
def _via(url: str, port: int) -> str:
    parts = urlsplit(url)
    userinfo = parts.netloc.rsplit("@", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{port}"))


async def test_database_outage_pauses_then_recovers_exactly_once(
    kafka_bootstrap: str,
    span_topic: str,
    build: Callable[..., SpanConsumer],
    sessions: Sessions,
    worker_db_url: str,
    tenant_id: uuid.UUID,
) -> None:
    target = urlsplit(worker_db_url)
    proxy = TcpProxy(target.hostname or "127.0.0.1", target.port or 5432)
    await proxy.start()
    engine = create_engine(
        _via(worker_db_url, proxy.port), pool_size=2, connect_timeout_s=2, command_timeout_s=5
    )
    consumer = build(sessions=create_session_factory(engine), backoff=Backoff(0.05, 1.0, 3))
    spans = [s for t in range(1, 11) for s in _chain(tenant_id, t, _now_ns(), ["x", "y"])]
    msgs = span_messages(tenant_id, spans, per_message=6)
    try:
        await proxy.cut()
        produce_many(kafka_bootstrap, span_topic, msgs)
        async with running(consumer):
            await wait_until(lambda: consumer.stats.pauses >= 1, timeout_s=30)
            assert consumer.stats.messages == 0  # nothing committed while the DB is down
            await proxy.restore()
            await wait_until(lambda: consumer.stats.messages == len(msgs), timeout_s=60)
    finally:
        await proxy.close()
        await engine.dispose()
    spans_n, edges, _ = await _state(sessions, tenant_id)
    assert (spans_n, edges) == (30, {"x->y": 10})
    assert consumer.stats.transient_errors >= 3


# ----------------------------------------------------------------------------- throughput
@pytest.mark.parametrize("batch_messages", [8, 100])
async def test_throughput_with_20k_generated_spans(
    kafka_bootstrap: str,
    span_topic: str,
    build: Callable[..., SpanConsumer],
    sessions: Sessions,
    tenant_id: uuid.UUID,
    batch_messages: int,
) -> None:
    """~21.6k spans of the demo topology in 512-span messages (one Collector batch each).

    Two numbers are printed: end to end (includes the consumer group join, a few seconds) and
    per busy second (time spent handling batches: parse, tenant check, insert, derive, commit).
    """
    scenario = replace(
        tg.control(), tenant_id=str(tenant_id), start_minutes=-13.0, end_minutes=13.0
    )
    spans = tg.generate(scenario, seed=7, deployed_at=datetime.now(UTC) - timedelta(minutes=15))
    assert 18_000 <= len(spans) <= 24_000
    msgs = span_messages(tenant_id, spans, per_message=512)  # a Collector batch per message
    produce_many(kafka_bootstrap, span_topic, msgs)

    consumer = build(batch_messages=batch_messages)
    started = time.perf_counter()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.spans_inserted == len(spans), timeout_s=120)
        elapsed = time.perf_counter() - started
    tag = f"span_consumer[batch_messages={batch_messages}]"
    measure(f"{tag}.spans", len(spans))
    measure(f"{tag}.messages", len(msgs))
    measure(f"{tag}.batches", consumer.stats.batches)
    measure(f"{tag}.end_to_end_s", elapsed, "s")
    measure(f"{tag}.spans_per_s_end_to_end", len(spans) / elapsed)
    measure(f"{tag}.busy_s", consumer.stats.busy_s, "s")
    measure(f"{tag}.spans_per_s_busy", len(spans) / consumer.stats.busy_s)
    spans_n, edges, (requests, _, _) = await _state(sessions, tenant_id)
    assert spans_n == len(spans)
    assert requests == sum(1 for s in spans if s.kind is SpanKind.SERVER)
    assert set(edges) == {
        "gateway->orders",
        "orders->inventory",
        "orders->payment",
        "payment->fraud",
    }
