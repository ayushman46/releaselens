"""Analysis job runner: claims due ``analysis_jobs`` and dispatches them by kind (ADR-0011).

Each poll claims a small batch in one short transaction (``FOR UPDATE SKIP LOCKED``; see
:mod:`rl_db.admin.jobs`), then runs the jobs one by one, each outside any transaction.
The outcomes:

* the handler returns: ``succeeded``;
* the handler raises: ``failed`` with ``run_at`` pushed back by full-jitter backoff, or
  ``dead`` once ``max_attempts`` is used up;
* the handler exceeds ``job_timeout``: counted as a failure;
* shutdown interrupts it: handed back as ``pending``, without using up an attempt. That
  covers a stop request between jobs and a cancellation (the supervisor's last resort):
  the running job and every claimed job that has not started yet are all handed back.

The lease of each job is restarted when its handler starts (``renew_lease``), because jobs
later in a batch wait for the earlier ones. A job whose claim was lost meanwhile (its lease
expired and another runner took it) is skipped, so it never runs twice concurrently.

Only kinds with a registered handler are ever claimed; a kind without one stays ``pending``
instead of being marked done by a runner that cannot do it. The worker registers the
regression analysis (:mod:`rl_worker.analysis`) for every kind, and starts the runner only
when the registry is non-empty.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.obs import bind_log_context, reset_log_context
from rl_db.admin.jobs import (
    ClaimedJob,
    claim_due_jobs,
    complete_job,
    fail_job,
    reap_expired_leases,
    release_job,
    renew_lease,
)
from rl_db.models import JobKind, JobStatus
from rl_worker.retry import Backoff

log = logging.getLogger("rl_worker.jobs")

JobHandler = Callable[[ClaimedJob], Awaitable[None]]


class HandlerRegistry:
    """Maps each :class:`JobKind` to the coroutine that performs it."""

    def __init__(self) -> None:
        self._handlers: dict[JobKind, JobHandler] = {}

    def register(self, kind: JobKind, handler: JobHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"a handler for {kind.value!r} is already registered")
        self._handlers[kind] = handler

    def get(self, kind: JobKind) -> JobHandler | None:
        return self._handlers.get(kind)

    @property
    def kinds(self) -> frozenset[JobKind]:
        return frozenset(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)


def default_registry(analysis_handler: JobHandler) -> HandlerRegistry:
    """The handlers the worker process runs: the regression analysis for ``early_5m``,
    ``early_10m`` and ``final``."""
    registry = HandlerRegistry()
    for kind in JobKind:
        registry.register(kind, analysis_handler)
    return registry


@dataclass
class RunnerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead: int = 0
    released: int = 0
    lost: int = 0  # finished after the lease expired and another runner took the job


class JobRunner:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: HandlerRegistry,
        *,
        worker_id: str,
        batch_size: int = 10,
        poll_interval_s: float = 2.0,
        lease: timedelta = timedelta(minutes=10),
        job_timeout: timedelta = timedelta(minutes=5),
        backoff: Backoff | None = None,
    ) -> None:
        if job_timeout >= lease:
            # Otherwise a slow but healthy job could be re-claimed by another runner while
            # it is still running.
            raise ValueError("job_timeout must be shorter than the lease")
        self._sessions = sessions
        self._registry = registry
        self.worker_id = worker_id
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._lease = lease
        self._job_timeout_s = job_timeout.total_seconds()
        self._backoff = backoff or Backoff(base_s=10.0, cap_s=300.0)
        self.stats = RunnerStats()

    async def run_once(self, stop: asyncio.Event | None = None) -> int:
        """Claim one batch and run it. Returns the number of jobs claimed."""
        kinds = self._registry.kinds
        if not kinds:
            return 0
        async with self._sessions.begin() as session:
            reaped = await reap_expired_leases(session, lease=self._lease)
            jobs = await claim_due_jobs(
                session,
                worker_id=self.worker_id,
                kinds=kinds,
                limit=self._batch_size,
                lease=self._lease,
            )
        if reaped:
            log.warning("jobs marked dead after their final lease expired", extra={"n": reaped})
        self.stats.claimed += len(jobs)
        index = 0
        try:
            for index, job in enumerate(jobs):
                if stop is not None and stop.is_set():
                    await self._release(jobs[index:])
                    break
                await self._execute(job)
        except asyncio.CancelledError:
            # Hand back the interrupted job and the ones that never started. Otherwise they
            # stay ``running`` until their lease expires and each loses an attempt.
            try:
                await asyncio.shield(self._release(jobs[index:]))
            except Exception:
                log.warning("could not hand back claimed jobs on cancellation", exc_info=True)
            raise
        return len(jobs)

    async def _execute(self, job: ClaimedJob) -> None:
        handler = self._registry.get(job.kind)
        if handler is None:  # registry changed after the claim; give the job back
            await self._release([job])
            return
        token = bind_log_context(
            tenant_id=str(job.tenant_id), job_id=str(job.id), job_kind=job.kind.value
        )
        try:
            async with self._sessions.begin() as session:
                still_ours = await renew_lease(session, job.id, self.worker_id)
            if not still_ours:
                self.stats.lost += 1
                log.warning("analysis job skipped: its claim expired and another runner took it")
                return
            try:
                await asyncio.wait_for(handler(job), timeout=self._job_timeout_s)
            except Exception as exc:
                await self._fail(job, exc)
            else:
                async with self._sessions.begin() as session:
                    owned = await complete_job(session, job.id, self.worker_id)
                if owned:
                    self.stats.succeeded += 1
                    log.info("analysis job succeeded", extra={"attempt": job.attempts})
                else:
                    self.stats.lost += 1
                    log.warning("analysis job finished after its claim was lost")
        finally:
            reset_log_context(token)

    async def _fail(self, job: ClaimedJob, exc: BaseException) -> None:
        reason = (
            f"timed out after {self._job_timeout_s:.0f}s"
            if isinstance(exc, TimeoutError)
            else f"{type(exc).__name__}: {exc}"
        )
        delay = timedelta(seconds=self._backoff.delay(job.attempts))
        async with self._sessions.begin() as session:
            status = await fail_job(
                session, job.id, self.worker_id, error=reason, retry_delay=delay
            )
        if status is JobStatus.DEAD:
            self.stats.dead += 1
            log.error(
                "analysis job dead after max attempts",
                extra={"attempt": job.attempts, "error": reason},
                exc_info=exc,
            )
        elif status is JobStatus.FAILED:
            self.stats.failed += 1
            log.warning(
                "analysis job failed; will retry",
                extra={
                    "attempt": job.attempts,
                    "retry_in_s": delay.total_seconds(),
                    "error": reason,
                },
            )
        else:
            self.stats.lost += 1

    async def _release(self, jobs: list[ClaimedJob]) -> None:
        async with self._sessions.begin() as session:
            for job in jobs:
                if await release_job(session, job.id, self.worker_id):
                    self.stats.released += 1

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "job runner started",
            extra={"kinds": sorted(k.value for k in self._registry.kinds)},
        )
        while not stop.is_set():
            try:
                claimed = await self.run_once(stop)
            except Exception:
                # Database unavailable, most likely. Keep polling; claims are idempotent.
                log.warning("job runner poll failed", exc_info=True)
                claimed = 0
            if claimed < self._batch_size:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_s)
        log.info("job runner stopped", extra={"stats": vars(self.stats)})
