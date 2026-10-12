from __future__ import annotations

from collections.abc import Iterator

import pytest

from rl_db.testing import create_database, create_migrated_database, postgres_container


@pytest.fixture(scope="package")
def pg_server_url() -> Iterator[str]:
    """One throwaway Postgres 16 container (testcontainers) for this test package."""
    with postgres_container() as url:
        yield url


@pytest.fixture
def pg_url(pg_server_url: str) -> str:
    """An empty database of its own (for the migration round-trip test)."""
    return create_database(pg_server_url)


@pytest.fixture(scope="module")
def migrated_url(pg_server_url: str) -> str:
    """A database at Alembic ``head``, shared by the tests of one module."""
    return create_migrated_database(pg_server_url)
