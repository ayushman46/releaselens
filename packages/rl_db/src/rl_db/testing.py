"""Integration-test helpers: a throwaway Postgres and fresh, migrated databases.

Not imported by production code. ``testcontainers`` and ``asyncpg`` are imported lazily, so
importing this module never needs Docker.

The pattern is one container per test package and one *database* per test module (or per
test that needs a pristine schema). Creating a database takes milliseconds, while a
container takes seconds and about 30 MiB of the shared VM's memory.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

POSTGRES_IMAGE = "postgres:16.15-alpine"  # ADR-0014 pin


@contextmanager
def postgres_container(image: str = POSTGRES_IMAGE) -> Iterator[str]:
    """Start Postgres and yield an asyncpg SQLAlchemy URL to its ``rl`` database."""
    from testcontainers.community.postgres import PostgresContainer

    # Throwaway credentials for a container that lives only as long as the test session.
    with PostgresContainer(
        image,
        username="rl",
        password="rl",  # noqa: S106
        dbname="rl",
        driver=None,
    ) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        yield f"postgresql+asyncpg://rl:rl@{host}:{port}/rl"


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def create_database(server_url: str, prefix: str = "rl_test") -> str:
    """Create an empty database on the server behind ``server_url`` and return its URL."""
    import asyncpg

    name = f"{prefix}_{secrets.token_hex(6)}"  # [a-z0-9_] only, so safe to interpolate

    async def _create() -> None:
        dsn = server_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    return _with_database(server_url, name)


def create_migrated_database(server_url: str, prefix: str = "rl_test") -> str:
    """Create a database and upgrade it to Alembic ``head``. Must run outside an event loop."""
    from alembic import command

    from rl_db.migrate import make_config

    url = create_database(server_url, prefix)
    command.upgrade(make_config(url), "head")
    return url
