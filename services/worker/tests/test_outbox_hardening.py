"""Adversarial OutboxRelay cases: poison rows, racing relays and forged dedup headers.

Uses a database of its own: the relay publishes every tenant's rows by design.
"""

from __future__ import annotations

import asyncio
import collections
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from confluent_kafka import Message
from confluent_kafka.admin import AdminClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.models import OutboxEvent
from rl_db.repositories import add_outbox_event
from rl_worker.kafka import AsyncProducer
from rl_worker.kafka_admin import TopicSpec, bootstrap
from rl_worker.outbox import OUTBOX_ID_HEADER, OutboxRelay
from rl_worker.retry import Backoff
from rl_worker.testing import read_topic

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
def outbox_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_outbox_hard")


@pytest.fixture
async def db(outbox_db_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(outbox_db_url, pool_size=6)
    sessions = create_session_factory(engine)
    async with sessions.begin() as s:
        await s.execute(delete(OutboxEvent))
    yield sessions
    await engine.dispose()


@pytest.fixture
def results_topic(kafka_bootstrap: str) -> str:
    name = f"analysis.results.h{uuid.uuid4().hex[:10]}"
    bootstrap(
        AdminClient({"bootstrap.servers": kafka_bootstrap}),
        topics=(TopicSpec(name, 3, 24 * 3600 * 1000),),
    )
    return name


async def _tenant(db: Sessions) -> TenantContext:
    async with db.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)


def _ids(messages: Sequence[Message]) -> list[str]:
    """The ``rl-outbox-id`` of each message, failing if any carries zero or several."""
    out = []
    for msg in messages:
        values = [h[1] for h in (msg.headers() or []) if h[0] == OUTBOX_ID_HEADER]
        assert len(values) == 1, f"expected exactly one {OUTBOX_ID_HEADER} header: {values}"
        value = values[0]
        assert isinstance(value, bytes)
        out.append(value.decode())
    return out


async def test_rows_that_can_never_be_published_do_not_starve_the_others(
    db: Sessions, producer: AsyncProducer, kafka_bootstrap: str, results_topic: str
) -> None:
    """A full batch of rows the broker will never take (here: larger than the producer's
    1 MB limit, written before any size check existed) sits at the head of the queue. The
    relay must still get to the healthy row behind them, and must not re-try the poison
    rows on every round."""
    poisoned, healthy = await _tenant(db), await _tenant(db)
    async with db.begin() as s:
        for _ in range(3):
            await s.execute(
                insert(OutboxEvent).values(
                    id=uuid7(),
                    tenant_id=poisoned.tenant_id,
                    topic=results_topic,
                    key="poison",
                    payload={"tenant_id": str(poisoned.tenant_id), "pad": "x" * 1_100_000},
                    headers={},
                )
            )
    async with db.begin() as s:
        good = await add_outbox_event(
            s, healthy, results_topic, "fine", {"tenant_id": str(healthy.tenant_id)}
        )

    relay = OutboxRelay(db, producer, batch_size=3, backoff=Backoff(60.0, 60.0))
    rounds = [await relay.run_once() for _ in range(3)]

    assert [(r.published, r.failed) for r in rounds] == [(0, 3), (1, 0), (0, 0)]
    assert _ids(read_topic(kafka_bootstrap, results_topic, expect=1)) == [str(good)]
    async with db() as s:
        poison = list(await s.scalars(select(OutboxEvent).where(OutboxEvent.key == "poison")))
        db_now = await s.scalar(select(func.now()))
    assert db_now is not None
    for row in poison:
        assert (row.published_at, row.attempts) == (None, 1)  # tried once, not every round
        assert row.available_at > db_now
        assert row.last_error


async def test_racing_relays_publish_every_row_exactly_once(
    db: Sessions, producer: AsyncProducer, kafka_bootstrap: str, results_topic: str
) -> None:
    """Four relays with small batches drain 150 rows at the same time. SKIP LOCKED plus the
    claim transaction spanning the publish means no row is sent by two relays."""
    ctx = await _tenant(db)
    async with db.begin() as s:
        ids = [
            await add_outbox_event(
                s, ctx, results_topic, f"k{i % 9}", {"tenant_id": str(ctx.tenant_id), "n": i}
            )
            for i in range(150)
        ]
    relays = [OutboxRelay(db, producer, batch_size=7) for _ in range(4)]

    async def unpublished() -> int:
        async with db() as s:
            stmt = select(func.count()).where(OutboxEvent.published_at.is_(None))
            return int(await s.scalar(stmt) or 0)

    async def drain(relay: OutboxRelay) -> None:
        while await unpublished():
            await relay.run_once()
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(*(drain(r) for r in relays)), timeout=60)

    counts = collections.Counter(_ids(read_topic(kafka_bootstrap, results_topic, expect=150)))
    assert set(counts) == {str(i) for i in ids}
    assert set(counts.values()) == {1}, "a row was published by more than one relay"
    assert sum(r.published_total for r in relays) == 150
    assert sum(r.failed_total for r in relays) == 0
    assert len([r for r in relays if r.published_total]) > 1  # they really did race


async def test_writers_cannot_forge_relay_headers_or_unpublishable_rows(
    db: Sessions, producer: AsyncProducer, kafka_bootstrap: str, results_topic: str
) -> None:
    ctx = await _tenant(db)
    payload = {"tenant_id": str(ctx.tenant_id)}
    async with db.begin() as s:
        for headers in ({OUTBOX_ID_HEADER: "forged"}, {"Content-Type": "text/plain"}):
            with pytest.raises(ValueError, match="relay"):
                await add_outbox_event(s, ctx, results_topic, "k", payload, headers)
        with pytest.raises(ValueError, match="topic"):
            await add_outbox_event(s, ctx, "analysis results", "k", payload)
        with pytest.raises(ValueError, match="headers exceed"):
            await add_outbox_event(s, ctx, results_topic, "k", payload, {"h": "x" * 20_000})

    # A row written without those checks still goes out with exactly one, genuine, id.
    row_id = uuid7()
    async with db.begin() as s:
        await s.execute(
            insert(OutboxEvent).values(
                id=row_id,
                tenant_id=ctx.tenant_id,
                topic=results_topic,
                key="k",
                payload=payload,
                headers={OUTBOX_ID_HEADER: "forged", "rl-kind": "x"},
            )
        )
    assert (await OutboxRelay(db, producer).run_once()).published == 1
    [msg] = read_topic(kafka_bootstrap, results_topic, expect=1)
    assert _ids([msg]) == [str(row_id)]
    assert ("rl-kind", b"x") in (msg.headers() or [])


async def test_a_crash_between_broker_ack_and_db_commit_republishes_with_the_same_id(
    db: Sessions,
    producer: AsyncProducer,
    kafka_bootstrap: str,
    results_topic: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented at-least-once edge: the broker has the message, then the relay dies
    before ``published_at`` commits. The row must be sent again, carrying the *same*
    ``rl-outbox-id``, which is what lets consumers drop the duplicate."""
    from rl_worker import outbox as relay_module

    ctx = await _tenant(db)
    async with db.begin() as s:
        row_id = await add_outbox_event(
            s, ctx, results_topic, "k", {"tenant_id": str(ctx.tenant_id)}
        )

    real_mark = relay_module.mark_published

    async def crash(*_args: object, **_kwargs: object) -> int:
        raise ConnectionResetError("database connection lost before commit")

    monkeypatch.setattr(relay_module, "mark_published", crash)
    with pytest.raises(ConnectionResetError):
        await OutboxRelay(db, producer).run_once()
    async with db() as s:
        row = (await s.scalars(select(OutboxEvent).where(OutboxEvent.id == row_id))).one()
    assert (row.published_at, row.attempts) == (None, 0)  # the whole round rolled back

    monkeypatch.setattr(relay_module, "mark_published", real_mark)
    assert (await OutboxRelay(db, producer).run_once()).published == 1
    assert (await OutboxRelay(db, producer).run_once()).published == 0

    ids = _ids(read_topic(kafka_bootstrap, results_topic, expect=2))
    assert ids == [str(row_id), str(row_id)]  # duplicated on the wire, same dedup id
    async with db() as s:
        row = (await s.scalars(select(OutboxEvent).where(OutboxEvent.id == row_id))).one()
    assert row.published_at is not None
    assert row.attempts == 1
