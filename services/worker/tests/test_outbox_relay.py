"""OutboxRelay against real Postgres + Kafka (test plan item g, outbox half).

Uses a database of its own: the relay publishes every tenant's rows by design.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from confluent_kafka.admin import AdminClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.models import OutboxEvent
from rl_db.repositories import add_outbox_event
from rl_worker.kafka import AsyncProducer
from rl_worker.kafka_admin import TopicSpec, bootstrap
from rl_worker.outbox import OUTBOX_ID_HEADER, OutboxRelay, RelayRound
from rl_worker.retry import Backoff
from rl_worker.testing import headers_of, read_topic, value_json

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
def outbox_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_outbox")


@pytest.fixture
async def db(outbox_db_url: str) -> AsyncIterator[Sessions]:
    engine = create_engine(outbox_db_url, pool_size=2)
    sessions = create_session_factory(engine)
    async with sessions.begin() as s:
        await s.execute(OutboxEvent.__table__.delete())
    yield sessions
    await engine.dispose()


@pytest.fixture
def results_topic(kafka_bootstrap: str) -> str:
    name = f"analysis.results.t{uuid.uuid4().hex[:10]}"
    bootstrap(
        AdminClient({"bootstrap.servers": kafka_bootstrap}),
        topics=(TopicSpec(name, 2, 24 * 3600 * 1000),),
    )
    return name


async def _tenant(db: Sessions) -> TenantContext:
    async with db.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
    return TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)


async def test_relay_publishes_exactly_the_unpublished_rows_and_is_idempotent(
    db: Sessions, producer: AsyncProducer, kafka_bootstrap: str, results_topic: str
) -> None:
    ctx = await _tenant(db)
    ids = []
    async with db.begin() as s:
        for i in range(4):
            ids.append(
                await add_outbox_event(
                    s,
                    ctx,
                    results_topic,
                    f"{ctx.tenant_id}:dep-{i}",
                    {"tenant_id": str(ctx.tenant_id), "n": i},
                    {"rl-event-type": "analysis.completed"},
                )
            )
        # Row 0 was already published by an earlier round: it must not be sent again.
        await s.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == ids[0])
            .values(published_at=OutboxEvent.created_at)
        )

    relay = OutboxRelay(db, producer, batch_size=100)
    first = await relay.run_once()
    second = await relay.run_once()
    assert (first.published, first.failed) == (3, 0)
    assert (second.published, second.failed) == (0, 0)

    messages = read_topic(kafka_bootstrap, results_topic, expect=3)
    assert len(messages) == 3  # nothing extra, nothing duplicated
    by_id = {headers_of(m)[OUTBOX_ID_HEADER].decode(): m for m in messages}
    assert set(by_id) == {str(i) for i in ids[1:]}
    for n, row_id in enumerate(ids[1:], start=1):
        msg = by_id[str(row_id)]
        assert msg.key() == f"{ctx.tenant_id}:dep-{n}".encode()
        assert value_json(msg) == {"tenant_id": str(ctx.tenant_id), "n": n}
        assert headers_of(msg)["rl-event-type"] == b"analysis.completed"
        assert headers_of(msg)["content-type"] == b"application/json"

    async with db() as s:
        rows = {r.id: r for r in await s.scalars(select(OutboxEvent))}
    assert all(r.published_at is not None for r in rows.values())
    assert [rows[i].attempts for i in ids] == [0, 1, 1, 1]


async def test_failed_publishes_keep_the_rows_for_the_next_round(
    db: Sessions, producer: AsyncProducer, kafka_bootstrap: str, results_topic: str
) -> None:
    ctx = await _tenant(db)
    async with db.begin() as s:
        for i in range(2):
            await add_outbox_event(
                s, ctx, results_topic, f"k{i}", {"tenant_id": str(ctx.tenant_id), "n": i}
            )

    dead_broker = AsyncProducer("127.0.0.1:1", client_id="dead", delivery_timeout_s=1.0)
    dead_broker.start()
    try:
        failed_round = await OutboxRelay(db, dead_broker, backoff=Backoff(60.0, 60.0)).run_once()
    finally:
        await asyncio.to_thread(dead_broker.close, 1)
    assert (failed_round.published, failed_round.failed) == (0, 2)
    async with db() as s:
        rows = list(await s.scalars(select(OutboxEvent)))
        db_now = await s.scalar(select(func.now()))
    assert all(r.published_at is None and r.attempts == 1 and r.last_error for r in rows)
    assert db_now is not None
    assert all(r.available_at > db_now for r in rows)  # pushed back by the retry backoff

    # Not retried before the backoff has passed, even with the broker back...
    assert await OutboxRelay(db, producer).run_once() == RelayRound(0, 0)
    async with db.begin() as s:  # ... and retried once it has (time travel, not sleep)
        await s.execute(update(OutboxEvent).values(available_at=func.now()))

    recovered = await OutboxRelay(db, producer).run_once()
    assert (recovered.published, recovered.failed) == (2, 0)
    assert len(read_topic(kafka_bootstrap, results_topic, expect=2)) == 2
    async with db() as s:
        rows = list(await s.scalars(select(OutboxEvent)))
    assert all(
        r.published_at is not None and r.attempts == 2 and r.last_error is None for r in rows
    )
