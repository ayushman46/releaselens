"""Outbox relay: publishes committed ``outbox`` rows to Kafka (ADR-0011).

Each round runs in one transaction:

1. lock up to ``batch_size`` of the oldest unpublished rows (``FOR UPDATE SKIP LOCKED``,
   so several relays can run without publishing the same row concurrently);
2. produce them all and wait for every broker acknowledgement;
3. set ``published_at`` on the acknowledged rows, and record ``attempts`` and
   ``last_error`` on the failed ones, which stay unpublished and are pushed back by a
   capped exponential backoff (``available_at``) before they are tried again;
4. commit.

The backoff is what keeps a row that can never be published (a deleted topic, a broker
that rejects it) from being claimed first on every round: it would otherwise be retried in
a hot loop and, once such rows fill a batch, starve every other tenant's messages.

A crash between 2 and 4 re-publishes those rows later, so delivery is at least once. Every
message carries ``rl-outbox-id`` (the row's UUIDv7) for consumers to deduplicate on.

Ordering: rows are produced oldest-first and the producer is idempotent, so messages with
the same key keep their order unless one of them fails while a later one succeeds. Consumers
of relayed topics must therefore tolerate reordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_db.admin.outbox import (
    PendingOutboxEvent,
    claim_unpublished,
    mark_failed,
    mark_published,
)
from rl_db.models.outbox import OUTBOX_ID_HEADER, RESERVED_HEADERS
from rl_worker.kafka import AsyncProducer, OutgoingMessage, ProduceError
from rl_worker.retry import Backoff

log = logging.getLogger("rl_worker.outbox")

__all__ = ["OUTBOX_ID_HEADER", "OutboxRelay", "RelayRound"]


@dataclass(frozen=True, slots=True)
class RelayRound:
    published: int
    failed: int


def _to_message(row: PendingOutboxEvent) -> OutgoingMessage:
    # Writers cannot set the relay's own headers (add_outbox_event refuses them); dropping
    # them here as well keeps a forged ``rl-outbox-id`` off the wire whatever wrote the row.
    headers = [
        (str(k), str(v).encode())
        for k, v in row.headers.items()
        if str(k).lower() not in RESERVED_HEADERS
    ]
    headers += [
        ("content-type", b"application/json"),
        (OUTBOX_ID_HEADER, str(row.id).encode()),
    ]
    value = json.dumps(row.payload, separators=(",", ":"), ensure_ascii=False).encode()
    return OutgoingMessage(topic=row.topic, key=row.key, value=value, headers=headers)


class OutboxRelay:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        producer: AsyncProducer,
        *,
        batch_size: int = 100,
        poll_interval_s: float = 1.0,
        backoff: Backoff | None = None,
    ) -> None:
        self._sessions = sessions
        self._producer = producer
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        # Per-row retry delay: 1 s, 2 s, 4 s … capped at a minute. The ceiling (no jitter) is
        # used because a single relay gains nothing from spreading its own retries.
        self._backoff = backoff or Backoff(base_s=1.0, cap_s=60.0)
        self.published_total = 0
        self.failed_total = 0

    async def run_once(self) -> RelayRound:
        async with self._sessions.begin() as session:
            rows = await claim_unpublished(session, limit=self._batch_size)
            if not rows:
                return RelayRound(0, 0)
            outcomes = await self._producer.produce_many([_to_message(r) for r in rows])
            delivered = [
                r.id for r, o in zip(rows, outcomes, strict=True) if not isinstance(o, ProduceError)
            ]
            failures = [
                (r, o) for r, o in zip(rows, outcomes, strict=True) if isinstance(o, ProduceError)
            ]
            await mark_published(session, delivered)
            for row, error in failures:
                delay = timedelta(seconds=self._backoff.ceiling(row.attempts + 1))
                await mark_failed(session, row.id, str(error), retry_delay=delay)
        self.published_total += len(delivered)
        self.failed_total += len(failures)
        if failures:
            log.warning(
                "outbox rows not published; will retry",
                extra={
                    "failed": len(failures),
                    "published": len(delivered),
                    "error": str(failures[0][1]),
                },
            )
        return RelayRound(published=len(delivered), failed=len(failures))

    async def run(self, stop: asyncio.Event) -> None:
        log.info("outbox relay started")
        while not stop.is_set():
            try:
                result = await self.run_once()
            except Exception:
                log.warning("outbox relay round failed", exc_info=True)
                result = RelayRound(0, 0)
            # A full batch means a backlog: go again immediately. Otherwise wait.
            if result.published + result.failed < self._batch_size or result.failed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_s)
        log.info(
            "outbox relay stopped",
            extra={"published": self.published_total, "failed": self.failed_total},
        )
