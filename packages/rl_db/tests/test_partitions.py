"""``spans`` is a range-partitioned table; partitions are created ahead and dropped for retention.

Alembic's autogenerate does not model ``PARTITION BY``, so the structure the migration creates is
checked here directly from the catalog (the models-equal-migrations test covers columns, keys and
indexes, and passes with partitions present).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import rl_db.models  # noqa: F401
from rl_db.admin.partitions import (
    autogenerate_include_object,
    drop_partitions_older_than,
    ensure_partitions,
    ensure_partitions_for_days,
    list_partitions,
    partition_name,
)
from rl_db.base import Base

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 13)


@pytest.fixture
async def engine(pg_server_url: str) -> AsyncIterator[AsyncEngine]:
    """A fresh migrated database per test: partitions are global state."""
    from rl_db.testing import create_migrated_database

    url = await asyncio.to_thread(create_migrated_database, pg_server_url, "rl_part")
    eng = create_async_engine(url)
    yield eng
    await eng.dispose()


async def test_spans_is_range_partitioned_on_start_time(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT c.relkind::text, pg_get_partkeydef(c.oid) FROM pg_class c "
                    "WHERE c.oid = 'spans'::regclass"
                )
            )
        ).one()
        assert tuple(row) == ("p", "RANGE (start_time)")
        pk = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'spans'::regclass AND contype = 'p'"
                )
            )
        ).scalar_one()
        assert pk == "PRIMARY KEY (tenant_id, trace_id, span_id, start_time)"
        indexes = {
            r[0]: r[1]
            for r in await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'spans'")
            )
        }
        assert (
            "(tenant_id, environment, service, start_time)"
            in (indexes["ix_spans_tenant_env_service_start_time"])
        )
        assert "(tenant_id, trace_id, span_id, start_time)" in indexes["pk_spans"]
        # No default partition: every row must land in a day partition.
        assert await list_partitions(conn) == []


async def test_ensure_is_idempotent_and_creates_today_plus_days_ahead(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        created = await ensure_partitions(conn, days_ahead=2, today=TODAY)
    assert created == [TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=2)]
    async with engine.begin() as conn:
        assert await ensure_partitions(conn, days_ahead=2, today=TODAY) == []
        assert await ensure_partitions_for_days(conn, [TODAY, TODAY]) == []
        assert await list_partitions(conn) == created
        bound = (
            await conn.execute(
                text("SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE relname = :n"),
                {"n": partition_name(TODAY)},
            )
        ).scalar_one()
    assert bound == ("FOR VALUES FROM ('2026-10-13 00:00:00+00') TO ('2026-10-14 00:00:00+00')")


async def test_rows_route_to_their_day_and_missing_days_are_rejected(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await ensure_partitions_for_days(conn, [TODAY])
    insert = text(
        "INSERT INTO spans (tenant_id, trace_id, span_id, service, name, kind, start_ns, end_ns, "
        "duration_ms, status_code, start_time) VALUES (:t, 'a', 'b', 's', 'n', 'SERVER', 1, 2, "
        "0.0, 'UNSET', :st)"
    )
    async with engine.begin() as conn:
        await conn.execute(
            insert, {"t": uuid.uuid4(), "st": datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC)}
        )
        where = (
            await conn.execute(text("SELECT tableoid::regclass::text FROM spans"))
        ).scalar_one()
    assert where == partition_name(TODAY)
    with pytest.raises(DBAPIError, match="no partition"):
        async with engine.begin() as conn:
            await conn.execute(
                insert, {"t": uuid.uuid4(), "st": datetime(2026, 10, 14, 0, 0, tzinfo=UTC)}
            )


async def test_concurrent_ensures_do_not_collide(engine: AsyncEngine) -> None:
    days = [TODAY + timedelta(days=n) for n in range(5)]

    async def ensure() -> list[date]:
        async with engine.begin() as conn:
            return await ensure_partitions_for_days(conn, days)

    results = await asyncio.gather(*(ensure() for _ in range(6)))
    assert sorted(d for r in results for d in r) == days  # each day created exactly once
    async with engine.connect() as conn:
        assert await list_partitions(conn) == days


async def test_retention_drops_only_whole_days_older_than_the_cutoff(engine: AsyncEngine) -> None:
    days = [TODAY - timedelta(days=n) for n in range(10, -1, -1)]  # 10 days ago .. today
    async with engine.begin() as conn:
        await ensure_partitions_for_days(conn, days)
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    async with engine.begin() as conn:
        dropped = await drop_partitions_older_than(conn, 7, now=now)
    # Cutoff 2026-10-06 12:00: a partition goes once its *end* (next midnight) is <= cutoff.
    assert dropped == [TODAY - timedelta(days=n) for n in range(10, 7, -1)]
    async with engine.begin() as conn:
        assert await drop_partitions_older_than(conn, 7, now=now) == []  # idempotent
        assert (await list_partitions(conn))[0] == TODAY - timedelta(days=7)
    with pytest.raises(ValueError):
        async with engine.begin() as conn:
            await drop_partitions_older_than(conn, 0)


def _diff(conn: Connection, *, hook: bool) -> list[object]:
    opts: dict[str, object] = {"compare_type": True}
    if hook:
        opts["include_object"] = autogenerate_include_object
    ctx = MigrationContext.configure(conn, opts=opts)
    return list(compare_metadata(ctx, Base.metadata))


async def test_autogenerate_ignores_partitions_but_still_compares_the_parent(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        await ensure_partitions(conn, days_ahead=1, today=TODAY)
    async with engine.connect() as conn:
        raw = await conn.run_sync(lambda c: _diff(c, hook=False))
        filtered = await conn.run_sync(lambda c: _diff(c, hook=True))
    # Without the env.py hook, autogenerate would propose dropping the runtime partitions.
    assert {d[0] for d in raw} == {"remove_table", "remove_index"}  # type: ignore[index]
    assert filtered == [], f"models and migrations disagree: {filtered}"

    # The parent is still compared: drift on spans itself is reported.
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX ix_spans_tenant_env_service_start_time"))
    async with engine.connect() as conn:
        drift = await conn.run_sync(lambda c: _diff(c, hook=True))
    assert [d[0] for d in drift] == ["add_index"]  # type: ignore[index]
