"""Telemetry maintenance loop: span partitions ahead of time, and retention.

Every ``interval_s`` (and once at start):

* create the ``spans`` partitions for today and the next ``days_ahead`` days (UTC), so the
  consumer rarely creates one on its hot path;
* drop span partitions whose whole day is older than ``span_retention_days`` (ADR-0008:
  retention is a partition drop, never a bulk DELETE);
* delete minute rollups older than ``rollup_retention_days``.

Every step is idempotent and safe with several workers (advisory lock, ``IF EXISTS``). A failed
round is logged and retried at the next interval; it never stops the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_db.admin.partitions import drop_partitions_older_than, ensure_partitions
from rl_db.admin.telemetry import delete_rollups_older_than

log = logging.getLogger("rl_worker.maintenance")


@dataclass(frozen=True, slots=True)
class MaintenanceRound:
    created: int
    dropped: int
    rollups_deleted: int


class TelemetryMaintenance:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        interval_s: float = 3600.0,
        days_ahead: int = 2,
        span_retention_days: int = 7,
        rollup_retention_days: int = 30,
    ) -> None:
        if span_retention_days < 1 or rollup_retention_days < 1:
            raise ValueError("retention must be at least one day")
        self._sessions = sessions
        self._interval_s = interval_s
        self._days_ahead = days_ahead
        self._span_retention_days = span_retention_days
        self._rollup_retention_days = rollup_retention_days
        self.last_round: MaintenanceRound | None = None

    async def run_once(self) -> MaintenanceRound:
        async with self._sessions.begin() as session:
            created = await ensure_partitions(session, days_ahead=self._days_ahead)
        async with self._sessions.begin() as session:
            dropped = await drop_partitions_older_than(session, self._span_retention_days)
        async with self._sessions.begin() as session:
            deleted = await delete_rollups_older_than(session, self._rollup_retention_days)
        result = MaintenanceRound(len(created), len(dropped), deleted)
        self.last_round = result
        if created or dropped or deleted:
            log.info(
                "telemetry maintenance",
                extra={
                    "partitions_created": [d.isoformat() for d in created],
                    "partitions_dropped": [d.isoformat() for d in dropped],
                    "rollups_deleted": deleted,
                },
            )
        return result

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.warning("telemetry maintenance failed; will retry", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
