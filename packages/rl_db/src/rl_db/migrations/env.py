"""Alembic environment (async, asyncpg). The URL comes from ``RL_DATABASE_URL`` or the config."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import rl_db.models  # noqa: F401  (registers tables)
from rl_db.admin.partitions import autogenerate_include_object
from rl_db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("RL_DATABASE_URL")
    if not url:
        raise RuntimeError("Set RL_DATABASE_URL (postgresql+asyncpg://…) to run migrations")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=autogenerate_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # spans' daily partitions are runtime objects; see rl_db.admin.partitions.
        include_object=autogenerate_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
