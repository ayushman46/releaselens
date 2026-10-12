"""Adversarial cases for DeploymentConsumer: inputs built to stall a partition or forge a tenant.

Each test here targets one way the at-least-once loop could misbehave on hostile or broken
input or events: retrying a message for ever (a poison message stalling its partition), a
partition left paused for ever after a rebalance, or persisting an event under a tenant
that a smuggled field names.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit, urlunsplit

import pytest
from confluent_kafka import Consumer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import MAX_ENVELOPE_BYTES, dlq
from rl_db import create_engine, create_session_factory
from rl_db.models import Deployment
from rl_worker.consumers.deployments import DeploymentConsumer
from rl_worker.kafka import AsyncProducer
from rl_worker.retry import Backoff
from rl_worker.testing import (
    TcpProxy,
    committed_vs_end,
    headers_of,
    make_event,
    produce_raw,
    read_topic,
    wait_until,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
FAST = Backoff(base_s=0.05, cap_s=0.3, max_attempts=2)


@pytest.fixture
def build(
    kafka_bootstrap: str, sessions: Sessions, producer: AsyncProducer, topic: str
) -> Callable[[], DeploymentConsumer]:
    group = f"rl-hardening-{uuid.uuid4().hex[:8]}"

    def _build() -> DeploymentConsumer:
        return DeploymentConsumer(
            bootstrap_servers=kafka_bootstrap,
            sessions=sessions,
            producer=producer,
            client_id=f"test-{uuid.uuid4().hex[:6]}",
            group_id=group,
            topic=topic,
            backoff=FAST,
            poll_timeout_s=0.2,
        )

    _build.group = group  # type: ignore[attr-defined]
    return _build


@contextlib.asynccontextmanager
async def running(consumer: DeploymentConsumer) -> AsyncIterator[asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        yield task
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


async def _count(sessions: Sessions, *where: object) -> int:
    async with sessions() as s:
        stmt = select(func.count()).select_from(Deployment).where(*where)  # type: ignore[arg-type]
        return int(await s.scalar(stmt) or 0)


def _fully_committed(bootstrap: str, group: str, topic: str) -> bool:
    return all(c == end for c, end in committed_vs_end(bootstrap, group, topic).values() if end)


async def test_an_event_too_large_for_its_dlq_copy_does_not_stall_the_partition(
    kafka_bootstrap: str,
    topic: str,
    build: Callable[[], DeploymentConsumer],
    sessions: Sessions,
    tenant_id: uuid.UUID,
) -> None:
    """A value just under the producer's 1 MB limit is accepted on the source topic, but the
    DLQ copy adds metadata headers and would cross that limit. The DLQ write must not fail
    for ever (classified transient, so the partition would be retried and paused
    indefinitely); the oversized body is unreplayable anyway (it exceeds
    MAX_ENVELOPE_BYTES), so the DLQ keeps a bounded prefix and says so."""
    key = f"{tenant_id}:payment"
    huge = b'{"pad": "' + b"x" * 999_850 + b'"}'
    good_id, good = make_event(tenant_id)
    produce_raw(kafka_bootstrap, topic, [(key, huge), (key, good)])

    consumer = build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.committed == 2, timeout_s=30)

    assert consumer.stats.dead_lettered == 1
    assert consumer.stats.pauses == 0
    assert await _count(sessions, Deployment.event_id == good_id) == 1
    [dead] = read_topic(kafka_bootstrap, dlq(topic), expect=1)
    headers = headers_of(dead)
    assert headers["error_type"] == b"event_too_large"
    assert headers["original_value_bytes"] == str(len(huge)).encode()
    assert headers["value_truncated"] == b"true"
    value = dead.value() or b""
    assert huge.startswith(value)
    assert MAX_ENVELOPE_BYTES <= len(value) < len(huge)
    assert _fully_committed(kafka_bootstrap, build.group, topic)  # type: ignore[attr-defined]


async def test_payload_tenant_cannot_be_spoofed_by_a_nested_or_duplicate_field(
    kafka_bootstrap: str,
    topic: str,
    build: Callable[[], DeploymentConsumer],
    sessions: Sessions,
    tenant_id: uuid.UUID,
) -> None:
    """Two classic smuggling tricks: a ``tenant_id`` inside ``payload`` (unknown field) and a
    duplicated top-level ``tenant_id`` key (parsers disagree on which wins). Both must be
    rejected outright, never persisted under either tenant."""
    other = uuid.uuid4()
    _, raw = make_event(tenant_id)
    nested = json.loads(raw)
    nested["payload"]["tenant_id"] = str(other)
    text = raw.decode()
    duplicated = text[:-1] + f', "tenant_id": "{other}"}}'
    key = f"{tenant_id}:payment"
    produce_raw(
        kafka_bootstrap, topic, [(key, json.dumps(nested).encode()), (key, duplicated.encode())]
    )

    consumer = build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.committed == 2, timeout_s=30)

    assert consumer.stats.dead_lettered == 2
    assert consumer.stats.created == 0
    assert await _count(sessions, Deployment.tenant_id == other) == 0
    dead = read_topic(kafka_bootstrap, dlq(topic), expect=2)
    assert [headers_of(m)["error_type"] for m in dead] == [b"validation_error", b"malformed_json"]


def _via(url: str, port: int) -> str:
    parts = urlsplit(url)
    userinfo = parts.netloc.rsplit("@", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{port}"))


def _join_and_leave(bootstrap: str, group: str, topic: str) -> None:
    """A second group member that joins, receives its share, and leaves without consuming
    or committing anything: two eager rebalances, after which every partition is back with
    the first member. This is what a rolling deploy of the worker does to its peers."""
    member = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "enable.auto.commit": False,
            "session.timeout.ms": 6000,
            "heartbeat.interval.ms": 1000,
        }
    )
    assigned: list[int] = []
    member.subscribe(
        [topic], on_assign=lambda _c, parts: assigned.extend(p.partition for p in parts)
    )
    try:
        deadline = time.monotonic() + 20
        while not assigned and time.monotonic() < deadline:
            member.poll(0.1)
        assert assigned, "the extra member never received partitions"
    finally:
        member.close()


async def test_a_partition_paused_by_an_outage_resumes_after_a_rebalance(
    kafka_bootstrap: str,
    topic: str,
    sessions: Sessions,
    producer: AsyncProducer,
    tenant_id: uuid.UUID,
    worker_db_url: str,
) -> None:
    """librdkafka keeps an application pause on a partition across an eager rebalance that
    gives the partition back to the same member (measured on librdkafka 2.16). The
    consumer forgets its resume schedule on every rebalance, so unless a new assignment is
    explicitly resumed, a partition paused during a database blip stays paused for ever:
    no errors, no lag progress, readiness still green."""
    target = urlsplit(worker_db_url)
    proxy = TcpProxy(target.hostname or "127.0.0.1", target.port or 5432)
    await proxy.start()
    engine = create_engine(
        _via(worker_db_url, proxy.port), pool_size=2, connect_timeout_s=2, command_timeout_s=5
    )
    group = f"rl-rebalance-{uuid.uuid4().hex[:8]}"
    # A long pause (10..20 s), so that only the rebalance can explain a prompt recovery.
    consumer = DeploymentConsumer(
        bootstrap_servers=kafka_bootstrap,
        sessions=create_session_factory(engine),
        producer=producer,
        client_id=f"test-{uuid.uuid4().hex[:6]}",
        group_id=group,
        topic=topic,
        backoff=Backoff(base_s=0.05, cap_s=20.0, max_attempts=2),
        poll_timeout_s=0.2,
        consumer_overrides={"session.timeout.ms": 6000, "heartbeat.interval.ms": 1000},
    )
    events = [make_event(tenant_id, service=f"svc-{i}") for i in range(6)]
    try:
        async with running(consumer):
            await wait_until(lambda: consumer.ready, timeout_s=30)
            await proxy.cut()
            produce_raw(
                kafka_bootstrap,
                topic,
                [(f"{tenant_id}:svc-{i}", raw) for i, (_, raw) in enumerate(events)],
            )
            await wait_until(lambda: consumer.stats.pauses >= 1, timeout_s=30)
            await proxy.restore()

            await asyncio.to_thread(_join_and_leave, kafka_bootstrap, group, topic)
            await wait_until(lambda: consumer.stats.committed == len(events), timeout_s=9)
    finally:
        await proxy.close()
        await engine.dispose()

    assert consumer.stats.created == len(events)
    ids = [event_id for event_id, _ in events]
    assert await _count(sessions, Deployment.event_id.in_(ids)) == len(events)
    assert _fully_committed(kafka_bootstrap, group, topic)
