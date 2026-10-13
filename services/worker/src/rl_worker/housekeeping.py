"""Periodic housekeeping (M10): plan retention and expired security rows.

Every ``interval`` (first run one interval after start, so a restart loop cannot hammer the
database): purge deployments older than their organization's plan retention
(:func:`rl_db.admin.retention.purge_expired_deployments`, in bounded batches), drop ended
sessions, used/expired one-time tokens and stale OAuth states, idle rate-limit buckets, and
GitHub webhook delivery ids older than the redelivery window (they exist only to deduplicate).
Failures are logged and retried on the next tick; housekeeping never stops the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_db.admin.github import delete_old_deliveries
from rl_db.admin.ratelimit import delete_idle_buckets
from rl_db.admin.retention import purge_expired_deployments, purge_stale_auth_rows

log = logging.getLogger("rl_worker.housekeeping")

MAX_PURGE_BATCHES = 100  # per tick: bounded work, the rest waits for the next tick


@dataclass
class MaintenanceResult:
    deployments_purged: int = 0
    auth_rows_purged: int = 0
    buckets_purged: int = 0
    webhook_deliveries_purged: int = 0


class Maintenance:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        interval_s: float = 3600.0,
        batch_size: int = 1000,
        auth_grace: timedelta = timedelta(days=7),
        bucket_idle: timedelta = timedelta(days=1),
        # GitHub redelivers automatically for a short while and manually for up to 3 days;
        # a week of delivery ids is comfortably past that.
        delivery_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self._sessions = sessions
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._auth_grace = auth_grace
        self._bucket_idle = bucket_idle
        self._delivery_ttl = delivery_ttl

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            if stop.is_set():
                break
            try:
                result = await self.run_once()
                log.info("maintenance done", extra=vars(result))
            except Exception:
                log.warning("maintenance failed; retrying next interval", exc_info=True)

    async def run_once(self) -> MaintenanceResult:
        result = MaintenanceResult()
        for _ in range(MAX_PURGE_BATCHES):
            async with self._sessions.begin() as session:
                n = await purge_expired_deployments(session, batch_size=self._batch_size)
            result.deployments_purged += n
            if n < self._batch_size:
                break
        async with self._sessions.begin() as session:
            result.auth_rows_purged = await purge_stale_auth_rows(session, grace=self._auth_grace)
            result.buckets_purged = await delete_idle_buckets(session, idle_for=self._bucket_idle)
            result.webhook_deliveries_purged = await delete_old_deliveries(
                session, older_than=self._delivery_ttl
            )
        return result
