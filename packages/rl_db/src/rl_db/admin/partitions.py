"""Daily partitions of ``spans``: create ahead of time, drop for retention (ADR-0008).

Partition ``spans_pYYYYMMDD`` holds ``start_time`` in ``[YYYY-MM-DD 00:00 UTC, next day 00:00
UTC)``. Both operations are idempotent and safe to run concurrently from several workers:

* :func:`ensure_partitions` / :func:`ensure_partitions_for_days` take a transaction-scoped
  advisory lock, then ``CREATE TABLE IF NOT EXISTS … PARTITION OF``. Without the lock, two
  sessions creating the same partition at once can still collide on the catalog's unique
  indexes (``IF NOT EXISTS`` is checked before the insert, not atomically with it).
* :func:`drop_partitions_older_than` drops whole partitions whose **upper bound** is at or before
  the cutoff, so a partition is dropped only when every row in it is older than the retention.

Cross-tenant by nature (a partition holds every tenant's spans of one day), hence this module
lives in :mod:`rl_db.admin`. Callers own the transaction and commit it. Run partition DDL in a
short transaction of its own, never inside the span-insert transaction: creating a partition
locks the parent table until commit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from rl_db.repositories._tenancy import cross_tenant

PARENT_TABLE: Final = "spans"
_PARTITION_RE: Final = re.compile(r"^spans_p(\d{8})$")
# pg_advisory_xact_lock(int4, int4) key: (namespace, object). Namespace 0x524C ("RL").
_LOCK_NAMESPACE: Final = 0x524C
_LOCK_PARTITIONS: Final = 1

Executor = AsyncSession | AsyncConnection


def is_partition_name(name: str | None) -> bool:
    return name is not None and _PARTITION_RE.match(name) is not None


def autogenerate_include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Alembic ``include_object`` hook: ignore the daily partitions of ``spans``.

    Partitions are created at runtime, not by migrations, so autogenerate must not propose
    dropping them (or their per-partition copies of the parent's indexes). The partitioned
    parent ``spans`` itself is still compared column by column.
    """
    if type_ == "table":
        return not (reflected and is_partition_name(name))
    if type_ == "index" and reflected:
        table = getattr(obj, "table", None)
        return not is_partition_name(getattr(table, "name", None))
    return True


def partition_name(day: date) -> str:
    return f"{PARENT_TABLE}_p{day:%Y%m%d}"


def day_of(value: datetime) -> date:
    return value.astimezone(UTC).date()


def _bounds(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


@cross_tenant
async def list_partitions(executor: Executor) -> list[date]:
    """Days that have a partition, oldest first (other children of ``spans`` are ignored)."""
    rows = await executor.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = to_regclass(:parent)"
        ),
        {"parent": PARENT_TABLE},
    )
    days = []
    for (name,) in rows:
        match = _PARTITION_RE.match(name)
        if match:
            days.append(datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=UTC).date())
    return sorted(days)


@cross_tenant
async def ensure_partitions_for_days(executor: Executor, days: Iterable[date]) -> list[date]:
    """Create the partitions of ``days`` that do not exist yet. Returns the days created."""
    wanted = sorted(set(days))
    if not wanted:
        return []
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :obj)"),
        {"ns": _LOCK_NAMESPACE, "obj": _LOCK_PARTITIONS},
    )
    existing = set(await list_partitions(executor))
    created = []
    for day in wanted:
        if day in existing:
            continue
        low, high = _bounds(day)
        # Identifiers and bounds come from a date, never from input: safe to interpolate.
        await executor.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {partition_name(day)} PARTITION OF {PARENT_TABLE} "
                f"FOR VALUES FROM ('{low}') TO ('{high}')"
            )
        )
        created.append(day)
    return created


@cross_tenant
async def ensure_partitions(
    executor: Executor, *, days_ahead: int = 2, today: date | None = None
) -> list[date]:
    """Partitions for today (UTC) and the next ``days_ahead`` days. Returns the days created."""
    if days_ahead < 0:
        raise ValueError("days_ahead must not be negative")
    first = today or datetime.now(UTC).date()
    return await ensure_partitions_for_days(
        executor, (first + timedelta(days=n) for n in range(days_ahead + 1))
    )


@cross_tenant
async def drop_partitions_older_than(
    executor: Executor, days: int, *, now: datetime | None = None
) -> list[date]:
    """Drop partitions whose whole day lies more than ``days`` days before ``now``.

    A partition for day ``D`` ends at ``D + 1 day``; it is dropped when that end is at or before
    ``now - days``. Returns the dropped days.
    """
    if days < 1:
        raise ValueError("retention must be at least one day")
    cutoff = (now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=days)
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :obj)"),
        {"ns": _LOCK_NAMESPACE, "obj": _LOCK_PARTITIONS},
    )
    dropped = []
    for day in await list_partitions(executor):
        end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
        if end <= cutoff:
            await executor.execute(text(f"DROP TABLE IF EXISTS {partition_name(day)}"))
            dropped.append(day)
    return dropped
