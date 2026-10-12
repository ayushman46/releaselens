"""Integration fixtures for the API: one Postgres and one Kafka broker per test package.

Containers are package-scoped so they are torn down when this package's tests finish,
rather than piling up until the end of a multi-package run on the 4 GiB Colima VM.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="package")
def pg_server_url() -> Iterator[str]:
    from rl_db.testing import postgres_container

    with postgres_container() as url:
        yield url


@pytest.fixture(scope="package")
def api_db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_api")


@pytest.fixture(scope="package")
def kafka_bootstrap() -> Iterator[str]:
    """A KRaft broker with the standard topics created by the production bootstrap code."""
    from confluent_kafka.admin import AdminClient

    from rl_worker.kafka_admin import bootstrap
    from rl_worker.testing import kafka_container

    with kafka_container() as servers:
        bootstrap(AdminClient({"bootstrap.servers": servers}))
        yield servers
