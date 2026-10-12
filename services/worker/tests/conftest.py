"""Fixtures for worker integration tests: Postgres + Kafka (package-scoped containers).

Isolation inside the shared containers:

* each consumer test gets its **own topic pair** (``deployment.events.<hex>`` and its
  ``.dlq``), created by the production bootstrap code, and its own consumer group;
* tests touching cross-tenant queues (job runner, outbox relay) get their **own database**,
  because those queues are global by design.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from confluent_kafka.admin import AdminClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rl_core.events import dlq


@pytest.fixture(scope="package")
def pg_server_url() -> Iterator[str]:
    from rl_db.testing import postgres_container

    with postgres_container() as url:
        yield url


@pytest.fixture(scope="package")
def worker_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_worker")


@pytest.fixture(scope="package")
def kafka_bootstrap() -> Iterator[str]:
    from rl_worker.kafka_admin import bootstrap
    from rl_worker.testing import kafka_container

    with kafka_container() as servers:
        bootstrap(AdminClient({"bootstrap.servers": servers}))
        yield servers


@pytest.fixture
def topic(kafka_bootstrap: str) -> str:
    """A fresh ``deployment.events``-like topic (3 partitions) plus its DLQ."""
    from rl_worker.kafka_admin import TopicSpec, bootstrap

    name = f"deployment.events.t{uuid.uuid4().hex[:10]}"
    day_ms = 24 * 3600 * 1000
    bootstrap(
        AdminClient({"bootstrap.servers": kafka_bootstrap}),
        topics=(TopicSpec(name, 3, day_ms), TopicSpec(dlq(name), 1, day_ms)),
    )
    return name


@pytest.fixture
async def engine(worker_db_url: str) -> AsyncIterator[AsyncEngine]:
    from rl_db import create_engine

    eng = create_engine(worker_db_url, pool_size=4)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    from rl_db import create_session_factory

    return create_session_factory(engine)


@pytest.fixture
async def tenant_id(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    from rl_db.admin.identity import create_organization

    async with sessions.begin() as s:
        org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
    return org.id


@pytest.fixture
async def producer(kafka_bootstrap: str) -> AsyncIterator[Any]:
    from rl_worker.kafka import AsyncProducer

    p = AsyncProducer(kafka_bootstrap, client_id="test-worker", delivery_timeout_s=10)
    p.start()
    yield p
    await asyncio.to_thread(p.close, 5)
