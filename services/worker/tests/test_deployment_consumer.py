"""DeploymentConsumer against real Kafka + Postgres: spec §10 failure scenarios a to e."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import textwrap
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import dlq
from rl_db import create_engine, create_session_factory
from rl_db.models import AnalysisJob, Deployment, JobKind
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
FAST = Backoff(base_s=0.05, cap_s=0.5, max_attempts=3)
# Short group timeouts so a killed member is evicted in seconds, not the 45 s default.
GROUP_TIMEOUTS = {"session.timeout.ms": 6000, "heartbeat.interval.ms": 1000}


def measure(name: str, value: float, unit: str = "s") -> None:
    """Measured numbers are printed with a fixed prefix so a run's log can be grepped."""
    print(f"[measure] {name}={value:.3f}{unit}", file=sys.stderr)


@dataclass
class Factory:
    group: str
    build: Callable[..., DeploymentConsumer]


@pytest.fixture
def consumers(
    kafka_bootstrap: str, sessions: Sessions, producer: AsyncProducer, topic: str
) -> Factory:
    group = f"rl-deployments-{uuid.uuid4().hex[:8]}"

    def build(
        *,
        cls: type[DeploymentConsumer] = DeploymentConsumer,
        sessions: Sessions = sessions,
        backoff: Backoff = FAST,
    ) -> DeploymentConsumer:
        return cls(
            bootstrap_servers=kafka_bootstrap,
            sessions=sessions,
            producer=producer,
            client_id=f"test-{uuid.uuid4().hex[:6]}",
            group_id=group,
            topic=topic,
            backoff=backoff,
            poll_timeout_s=0.2,
            consumer_overrides=dict(GROUP_TIMEOUTS),
        )

    return Factory(group=group, build=build)


@contextlib.asynccontextmanager
async def running(consumer: DeploymentConsumer) -> AsyncIterator[asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        yield task
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


async def _count(sessions: Sessions, model: Any, *where: Any) -> int:
    async with sessions() as s:
        return int(await s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _fully_committed(bootstrap: str, group: str, topic: str) -> bool:
    return all(c == end for c, end in committed_vs_end(bootstrap, group, topic).values() if end)


# ----------------------------------------------------------------------------- (a)
async def test_duplicate_delivery_yields_one_row_and_three_jobs(
    kafka_bootstrap: str, topic: str, consumers: Factory, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    event_id, raw = make_event(tenant_id)
    key = f"{tenant_id}:payment"
    produce_raw(kafka_bootstrap, topic, [(key, raw), (key, raw), (key, raw)])

    consumer = consumers.build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.committed == 3)

    assert (consumer.stats.created, consumer.stats.duplicates) == (1, 2)
    async with sessions() as s:
        [dep_id] = list(
            await s.scalars(select(Deployment.id).where(Deployment.event_id == event_id))
        )
    assert await _count(sessions, AnalysisJob, AnalysisJob.deployment_id == dep_id) == 3
    assert _fully_committed(kafka_bootstrap, consumers.group, topic)


# ----------------------------------------------------------------------------- (b)
class SimulatedCrashError(Exception):
    pass


class CrashBeforeOffsetCommit(DeploymentConsumer):
    """Persists (DB transaction committed) and then dies before committing the offset."""

    async def _commit(self, msg: Any) -> None:
        raise SimulatedCrashError


async def test_crash_after_db_commit_before_offset_commit_is_redelivered_once(
    kafka_bootstrap: str, topic: str, consumers: Factory, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    event_id, raw = make_event(tenant_id)
    produce_raw(kafka_bootstrap, topic, [(f"{tenant_id}:payment", raw)])

    victim = consumers.build(cls=CrashBeforeOffsetCommit)
    with pytest.raises(SimulatedCrashError):
        await asyncio.wait_for(victim.run(asyncio.Event()), timeout=30)
    assert victim.stats.created == 1
    assert await _count(sessions, Deployment, Deployment.event_id == event_id) == 1
    assert all(c < 0 for c, _ in committed_vs_end(kafka_bootstrap, consumers.group, topic).values())

    survivor = consumers.build()
    async with running(survivor):
        await wait_until(lambda: survivor.stats.committed == 1)
    assert (survivor.stats.created, survivor.stats.duplicates) == (0, 1)
    assert await _count(sessions, Deployment, Deployment.event_id == event_id) == 1
    assert await _count(sessions, AnalysisJob, AnalysisJob.tenant_id == tenant_id) == 3
    assert _fully_committed(kafka_bootstrap, consumers.group, topic)


_VICTIM = textwrap.dedent(
    """
    import asyncio, os
    from rl_db import create_engine, create_session_factory
    from rl_worker.consumers.deployments import DeploymentConsumer
    from rl_worker.kafka import AsyncProducer

    class DieBeforeOffsetCommit(DeploymentConsumer):
        async def _commit(self, msg):
            # Like SIGKILL: no offset commit, no LeaveGroup, no cleanup of any kind.
            os._exit(137)

    async def main():
        sessions = create_session_factory(create_engine(os.environ["DB"]))
        producer = AsyncProducer(os.environ["BOOT"], client_id="victim", delivery_timeout_s=5)
        producer.start()
        consumer = DieBeforeOffsetCommit(
            bootstrap_servers=os.environ["BOOT"], sessions=sessions, producer=producer,
            client_id="victim", group_id=os.environ["GROUP"], topic=os.environ["TOPIC"],
            consumer_overrides={"session.timeout.ms": 6000, "heartbeat.interval.ms": 1000},
        )
        await consumer.run(asyncio.Event())

    asyncio.run(main())
    """
)


async def test_hard_killed_process_before_offset_commit_is_redelivered_once(
    kafka_bootstrap: str,
    topic: str,
    consumers: Factory,
    sessions: Sessions,
    tenant_id: uuid.UUID,
    worker_db_url: str,
    tmp_path: Path,
) -> None:
    """Same property as above, with a real process death (``os._exit``) in a subprocess."""
    event_id, raw = make_event(tenant_id)
    produce_raw(kafka_bootstrap, topic, [(f"{tenant_id}:payment", raw)])
    script = tmp_path / "victim.py"
    script.write_text(_VICTIM)
    env = {
        **os.environ,
        "DB": worker_db_url,
        "BOOT": kafka_bootstrap,
        "GROUP": consumers.group,
        "TOPIC": topic,
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(script), env=env, stdout=asyncio.subprocess.DEVNULL
    )
    assert await asyncio.wait_for(proc.wait(), timeout=60) == 137
    assert await _count(sessions, Deployment, Deployment.event_id == event_id) == 1

    started = time.perf_counter()
    survivor = consumers.build()
    async with running(survivor):
        await wait_until(lambda: survivor.stats.committed == 1, timeout_s=60)
    measure("redelivery_after_hard_kill", time.perf_counter() - started)
    assert (survivor.stats.created, survivor.stats.duplicates) == (0, 1)
    assert await _count(sessions, Deployment, Deployment.event_id == event_id) == 1
    assert await _count(sessions, AnalysisJob, AnalysisJob.tenant_id == tenant_id) == 3


# ----------------------------------------------------------------------------- (c)
async def test_malformed_events_are_dead_lettered_with_metadata_and_committed(
    kafka_bootstrap: str, topic: str, consumers: Factory, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    key = f"{tenant_id}:payment"
    _, valid = make_event(tenant_id)
    as_json = json.loads(valid)
    unknown_version = json.dumps({**as_json, "schema_version": 99}).encode()
    missing_field = json.loads(valid)
    del missing_field["payload"]["commit_sha"]
    stranger, unknown_tenant = make_event(uuid.uuid4())  # well-formed, tenant not in the DB
    good_id, good = make_event(tenant_id)
    batch = [
        b'{"schema_version": 2, "event_id": ',
        unknown_version,
        json.dumps(missing_field).encode(),
        unknown_tenant,
        good,
    ]
    produce_raw(kafka_bootstrap, topic, [(key, v) for v in batch])

    consumer = consumers.build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.committed == len(batch))

    assert consumer.stats.dead_lettered == 4
    assert consumer.stats.created == 1  # the valid event after them was not blocked
    assert consumer.stats.transient_errors == 0
    dead = read_topic(kafka_bootstrap, dlq(topic), expect=4)
    assert len(dead) == 4
    headers = [headers_of(m) for m in dead]
    assert [h["error_type"] for h in headers] == [
        b"malformed_json",
        b"unknown_schema_version",
        b"validation_error",
        b"unknown_tenant",
    ]
    offsets = [int(h["original_offset"]) for h in headers]
    assert offsets == sorted(offsets)
    assert len({h["original_partition"] for h in headers}) == 1  # same key, same partition
    for msg, h, original in zip(dead, headers, batch[:4], strict=True):
        assert msg.value() == original  # original bytes, untouched
        assert msg.key() == key.encode()
        assert h["original_topic"] == topic.encode()
        assert h["error_message"]
        assert b"Traceback" in h["stack"]
        assert datetime.fromisoformat(h["failed_at"].decode()).tzinfo is not None
    assert b"commit_sha" in headers[2]["error_message"]
    assert _fully_committed(kafka_bootstrap, consumers.group, topic)
    assert await _count(sessions, Deployment, Deployment.tenant_id == tenant_id) == 1
    assert await _count(sessions, Deployment, Deployment.event_id == stranger) == 0
    assert await _count(sessions, Deployment, Deployment.event_id == good_id) == 1


# ----------------------------------------------------------------------------- (d)
def _via(url: str, port: int) -> str:
    parts = urlsplit(url)
    userinfo = parts.netloc.rsplit("@", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{port}"))


async def test_database_outage_retries_pauses_then_recovers_without_loss_or_duplicates(
    kafka_bootstrap: str,
    topic: str,
    consumers: Factory,
    sessions: Sessions,
    tenant_id: uuid.UUID,
    worker_db_url: str,
) -> None:
    target = urlsplit(worker_db_url)
    proxy = TcpProxy(target.hostname or "127.0.0.1", target.port or 5432)
    await proxy.start()
    engine = create_engine(
        _via(worker_db_url, proxy.port), pool_size=2, connect_timeout_s=2, command_timeout_s=5
    )
    proxied = create_session_factory(engine)
    consumer = consumers.build(sessions=proxied, backoff=Backoff(0.05, 1.0, 3))
    try:
        async with running(consumer):
            first_id, first = make_event(tenant_id, service="svc-0")
            produce_raw(kafka_bootstrap, topic, [(f"{tenant_id}:svc-0", first)])
            await wait_until(lambda: consumer.stats.committed == 1)

            await proxy.cut()
            outage_start = time.perf_counter()
            during = [make_event(tenant_id, service=f"svc-{i}") for i in range(1, 7)]
            produce_raw(
                kafka_bootstrap,
                topic,
                [(f"{tenant_id}:svc-{i}", raw) for i, (_, raw) in enumerate(during, 1)],
            )
            await wait_until(lambda: consumer.stats.pauses >= 1, timeout_s=30)
            await asyncio.sleep(2.0)  # stay down a little longer: several retry/pause cycles
            assert consumer.stats.committed == 1  # nothing committed while the DB is down
            assert consumer.stats.dead_lettered == 0  # an outage is never a DLQ reason
            await proxy.restore()
            outage = time.perf_counter() - outage_start
            restored = time.perf_counter()
            await wait_until(lambda: consumer.stats.committed == 7, timeout_s=60)
            recovery = time.perf_counter() - restored
    finally:
        await proxy.close()
        await engine.dispose()

    measure("db_outage_duration", outage)
    measure("db_recovery_to_all_committed", recovery)
    print(f"[measure] outage_stats={consumer.stats}", file=sys.stderr)
    assert consumer.stats.transient_errors >= 3
    assert consumer.stats.created == 7
    assert consumer.stats.dead_lettered == 0
    ids = [first_id] + [event_id for event_id, _ in during]
    assert await _count(sessions, Deployment, Deployment.event_id.in_(ids)) == 7
    assert await _count(sessions, Deployment, Deployment.tenant_id == tenant_id) == 7
    assert await _count(sessions, AnalysisJob, AnalysisJob.tenant_id == tenant_id) == 21
    assert _fully_committed(kafka_bootstrap, consumers.group, topic)
    assert read_topic(kafka_bootstrap, dlq(topic), expect=0, timeout_s=1) == []


# ----------------------------------------------------------------------------- (e)
async def test_schema_v1_and_v2_are_both_persisted_correctly(
    kafka_bootstrap: str, topic: str, consumers: Factory, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    deployed = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)
    risk_id = uuid.uuid4()
    v1_id, v1 = make_event(tenant_id, version=1, service="orders", deployed_at=deployed)
    v2_id, v2 = make_event(
        tenant_id,
        version=2,
        service="payment",
        deployed_at=deployed,
        repository="acme/payment",
        pull_request=99,
        risk_assessment_id=risk_id,
        labels={"team": "payments"},
        ci_run_url="https://ci.example.com/runs/7",
    )
    produce_raw(kafka_bootstrap, topic, [(f"{tenant_id}:orders", v1), (f"{tenant_id}:payment", v2)])

    consumer = consumers.build()
    async with running(consumer):
        await wait_until(lambda: consumer.stats.committed == 2)

    async with sessions() as s:
        rows = {
            d.event_id: d
            for d in await s.scalars(select(Deployment).where(Deployment.tenant_id == tenant_id))
        }
        jobs = list(await s.scalars(select(AnalysisJob).where(AnalysisJob.tenant_id == tenant_id)))
    one, two = rows[v1_id], rows[v2_id]
    assert (one.schema_version, one.service, one.version, one.previous_version) == (
        1,
        "orders",
        "v2",
        "v1",
    )
    assert (one.repository, one.pull_request, one.risk_assessment_id, one.labels) == (
        None,
        None,
        None,
        {},
    )
    assert (two.schema_version, two.repository, two.pull_request) == (2, "acme/payment", 99)
    assert two.risk_assessment_id == risk_id
    assert two.labels == {"team": "payments"}
    assert two.ci_run_url == "https://ci.example.com/runs/7"
    assert one.deployed_at == two.deployed_at == deployed
    offsets = {(j.deployment_id, j.kind): j.run_at - deployed for j in jobs}
    for dep in (one, two):
        assert offsets[(dep.id, JobKind.EARLY_5M)] == timedelta(minutes=5)
        assert offsets[(dep.id, JobKind.EARLY_10M)] == timedelta(minutes=10)
        assert offsets[(dep.id, JobKind.FINAL)] == timedelta(minutes=15)


async def test_readiness_tracks_group_membership(
    kafka_bootstrap: str, topic: str, consumers: Factory
) -> None:
    consumer = consumers.build()
    assert not consumer.ready
    started = time.perf_counter()
    async with running(consumer):
        await wait_until(lambda: consumer.ready, timeout_s=30)
        measure("consumer_group_join", time.perf_counter() - started)
    assert not consumer.ready
