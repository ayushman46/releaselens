"""Migrations must (1) apply cleanly, (2) match the ORM models exactly, (3) fully reverse."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

import rl_db.models  # noqa: F401
from rl_db.base import Base
from rl_db.migrate import make_config

pytestmark = pytest.mark.integration


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return list(compare_metadata(ctx, Base.metadata))


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


async def _run(url: str, fn):  # type: ignore[no-untyped-def]
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(fn)
    finally:
        await engine.dispose()


def test_upgrade_matches_models_and_downgrade_reverses(pg_url: str) -> None:
    cfg = make_config(pg_url)

    command.upgrade(cfg, "head")
    tables = asyncio.run(_run(pg_url, _tables))
    assert {"organizations", "users", "memberships", "api_keys"} <= tables

    diff = asyncio.run(_run(pg_url, _diff))
    assert diff == [], f"models and migrations disagree: {diff}"

    command.downgrade(cfg, "base")
    assert asyncio.run(_run(pg_url, _tables)) <= {"alembic_version"}

    # Re-applying after a full downgrade must also work (idempotent round-trip).
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
