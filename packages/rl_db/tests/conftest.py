from __future__ import annotations

from collections.abc import Iterator

import pytest

POSTGRES_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A throwaway Postgres 16 (testcontainers). Yields an asyncpg SQLAlchemy URL."""
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        POSTGRES_IMAGE, username="rl", password="rl", dbname="rl", driver=None
    ) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        yield f"postgresql+asyncpg://rl:rl@{host}:{port}/rl"
