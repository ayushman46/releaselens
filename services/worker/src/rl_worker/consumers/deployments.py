"""``deployment.events`` consumer: at-least-once delivery, idempotent persistence (spec §10).

For each message, in order:

1. ``parse_envelope``: validate and upcast. Any failure here is permanent.
2. One Postgres transaction: ``INSERT … ON CONFLICT (tenant_id, event_id) DO NOTHING``
   and, only if a row was inserted, the three ``analysis_jobs``.
3. After that transaction commits, a **synchronous** commit of this message's offset + 1.

If the process dies between steps 2 and 3, the message is delivered again and step 2 is a
no-op. That is why at-least-once delivery yields exactly one deployment row.

Failure handling (classes from :mod:`rl_worker.errors`):

* **Permanent** (bad JSON, unknown version or type, schema violation, unknown tenant,
  unclassified bug): copy the message to ``<topic>.dlq`` with metadata headers, wait for
  the broker ack, then commit the offset. If the DLQ write itself fails, that is transient.
* **Transient** (database down, timeouts): retry in place with full-jitter backoff, up to
  ``max_attempts``. Then *seek back* to the message's offset, **pause** its partition and
  return to the poll loop. Polling keeps the consumer in its group (``max.poll.interval.ms``)
  while the paused partition delivers nothing. After ``pause_delay()`` the partition is
  resumed and the same offset is delivered again. Nothing is committed and nothing is
  skipped, and the waits never shrink into a tight loop.

Messages are processed one at a time, in partition order. Throughput comes from partitions
and consumer instances, not from concurrency inside one consumer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Message, TopicPartition
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import (
    DEPLOYMENT_EVENTS,
    EventError,
    PermanentError,
    TransientError,
    dlq,
    parse_envelope,
)
from rl_core.obs import bind_log_context, reset_log_context
from rl_core.tenancy import Principal, TenantContext
from rl_db.models import JobKind
from rl_db.repositories import RecordedDeployment, analysis_schedule, record_deployment
from rl_worker.dlq import dead_letter
from rl_worker.errors import classify
from rl_worker.kafka import AsyncProducer, ProduceError, consumer_config
from rl_worker.retry import Backoff

log = logging.getLogger("rl_worker.consumers.deployments")

DEFAULT_GROUP_ID = "rl-deployments"

# Commit failures that mean "this partition now belongs to someone else". The new owner
# re-reads from the last committed offset, and persistence is idempotent, so they are safe
# to drop.
_REASSIGNED = frozenset(
    {
        KafkaError.REBALANCE_IN_PROGRESS,
        KafkaError.ILLEGAL_GENERATION,
        KafkaError.UNKNOWN_MEMBER_ID,
        KafkaError._ASSIGNMENT_LOST,  # librdkafka local error
        KafkaError._STATE,  # commit while not in a group (shutting down)
    }
)


@dataclass
class ConsumerStats:
    """Counters for logs, tests and (later) metrics."""

    committed: int = 0  # messages whose offset was committed
    created: int = 0  # new deployments persisted
    duplicates: int = 0  # redeliveries that were no-ops
    dead_lettered: int = 0
    transient_errors: int = 0
    pauses: int = 0
    commit_failures: int = 0


def _position(msg: Message) -> tuple[str, int, int]:
    """Topic, partition and offset of a consumed (non-error) message, which always has all
    three; librdkafka's types just do not say so."""
    topic, partition, offset = msg.topic(), msg.partition(), msg.offset()
    if topic is None or partition is None or offset is None:
        raise ValueError("message has no position")
    return topic, partition, offset


class DeploymentConsumer:
    def __init__(
        self,
        *,
        bootstrap_servers: str,
        sessions: async_sessionmaker[AsyncSession],
        producer: AsyncProducer,
        client_id: str,
        group_id: str = DEFAULT_GROUP_ID,
        topic: str = DEPLOYMENT_EVENTS,
        backoff: Backoff | None = None,
        post_window_end: timedelta | None = None,
        poll_timeout_s: float = 1.0,
        consumer_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.topic = topic
        self.dlq_topic = dlq(topic)
        self._sessions = sessions
        self._producer = producer
        self._backoff = backoff or Backoff()
        self._schedule: Sequence[tuple[JobKind, timedelta]] = (
            analysis_schedule(post_window_end) if post_window_end else analysis_schedule()
        )
        self._poll_timeout_s = poll_timeout_s
        config = consumer_config(
            bootstrap_servers,
            group_id=group_id,
            client_id=client_id,
            overrides=consumer_overrides,
        )
        if "cooperative" in str(config.get("partition.assignment.strategy", "")):
            # The rebalance callbacks (assign + resume in _on_assign, clearing all pause
            # state on revoke) are written for eager rebalancing only.
            raise ValueError("DeploymentConsumer requires an eager partition assignment strategy")
        self._consumer = Consumer(config)
        # (topic, partition) -> monotonic time at which to resume it.
        self._paused: dict[tuple[str, int], float] = {}
        # The poll() running in a worker thread, if any (see _poll).
        self._inflight_poll: asyncio.Future[Message | None] | None = None
        self._joined = False
        self.stats = ConsumerStats()

    # ------------------------------------------------------------------ lifecycle
    @property
    def ready(self) -> bool:
        """True once the consumer has joined its group and received an assignment.

        An *empty* assignment still counts: with more consumers than partitions, an idle
        member is healthy, not broken.
        """
        return self._joined

    def _on_assign(self, consumer: Consumer, partitions: list[TopicPartition]) -> None:
        # Eager rebalancing: a new assignment starts from committed offsets, and it must
        # also start unpaused. librdkafka keeps an application pause on a partition across
        # a rebalance that hands the same partition back to this member (measured on
        # librdkafka 2.16), while the resume schedule below is dropped on every rebalance.
        # Without the explicit resume, a partition paused during a database blip and then
        # re-assigned here would stay paused for ever. Assigning inside the callback is
        # the documented way to act on the new assignment; the client then skips its own
        # assign() call.
        self._paused.clear()
        consumer.assign(partitions)
        consumer.resume(partitions)
        self._joined = True
        log.info(
            "partitions assigned",
            extra={"topic": self.topic, "partitions": sorted(p.partition for p in partitions)},
        )

    def _on_revoke(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        self._paused.clear()
        log.info(
            "partitions revoked",
            extra={"topic": self.topic, "partitions": sorted(p.partition for p in partitions)},
        )

    def _on_lost(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        self._paused.clear()
        self._joined = False
        log.warning("partitions lost", extra={"topic": self.topic})

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set. The in-flight message is finished first:
        persisted *and* committed, or (if its retries are interrupted) left uncommitted
        for redelivery. Then the consumer leaves the group cleanly."""
        self._consumer.subscribe(
            [self.topic],
            on_assign=self._on_assign,
            on_revoke=self._on_revoke,
            on_lost=self._on_lost,
        )
        log.info("deployment consumer started", extra={"topic": self.topic})
        try:
            while not stop.is_set():
                self._resume_due()
                msg = await self._poll()
                if msg is None:
                    continue
                err = msg.error()
                if err is not None:
                    if err.fatal():
                        raise KafkaException(err)
                    log.warning("kafka consumer error", extra={"error": err.str()})
                    continue
                await self._handle(msg, stop)
        finally:
            self._joined = False
            try:
                await self._wait_for_inflight_poll()
                # Leaves the group at once, so the partitions move without waiting for the
                # session timeout. Offsets are not committed (auto-commit is off).
                await asyncio.to_thread(self._consumer.close)
            except Exception:  # must not mask the exception that ended the loop
                log.warning("kafka consumer close failed", exc_info=True)
            log.info("deployment consumer stopped", extra={"stats": vars(self.stats)})

    async def _poll(self) -> Message | None:
        """``poll()`` in a worker thread, tracked so shutdown can wait for it.

        Cancelling the awaiting task does not stop the thread. librdkafka must not be
        closed while another thread is inside ``poll()`` on the same handle (undefined
        behaviour, typically a crash), so the thread's future is kept, shielded from the
        cancellation, and :meth:`run` waits for it before closing. A message returned to an
        already-cancelled poll is simply not processed; its offset is not committed, so it
        is delivered again.
        """
        self._inflight_poll = asyncio.ensure_future(
            asyncio.to_thread(self._consumer.poll, self._poll_timeout_s)
        )
        return await asyncio.shield(self._inflight_poll)

    async def _wait_for_inflight_poll(self) -> None:
        inflight = self._inflight_poll
        if inflight is None or inflight.done():
            return
        # poll() returns within its timeout; the margin covers a slow rebalance callback.
        done, _ = await asyncio.wait({inflight}, timeout=self._poll_timeout_s + 10)
        if not done:
            raise RuntimeError("kafka poll() did not return; refusing to close the consumer")

    # ------------------------------------------------------------------ per message
    async def _handle(self, msg: Message, stop: asyncio.Event) -> None:
        attempt = 0
        while True:
            try:
                await self._persist(msg)
                break
            except Exception as exc:
                error = classify(exc)
                if isinstance(error, PermanentError):
                    try:
                        await self._dead_letter(msg, error, exc)
                        break
                    except ProduceError as dlq_exc:
                        error = TransientError(f"dead-letter publish failed: {dlq_exc}")
                attempt += 1
                self.stats.transient_errors += 1
                log.warning(
                    "transient failure; will retry",
                    extra={
                        **self._where(msg),
                        "attempt": attempt,
                        "max_attempts": self._backoff.max_attempts,
                        "error": str(error),
                    },
                )
                if attempt >= self._backoff.max_attempts:
                    self._rewind_and_pause(msg)
                    return
                if not await self._sleep(self._backoff.delay(attempt), stop):
                    log.info("stopping during retries; message left for redelivery")
                    return
        await self._commit(msg)

    async def _persist(self, msg: Message) -> RecordedDeployment:
        event = parse_envelope(msg.value())
        token = bind_log_context(tenant_id=str(event.tenant_id), event_id=str(event.event_id))
        try:
            ctx = TenantContext(tenant_id=event.tenant_id, principal=Principal.SYSTEM)
            async with self._sessions.begin() as session:
                result = await record_deployment(session, ctx, event, schedule=self._schedule)
            if result.created:
                self.stats.created += 1
                log.info(
                    "deployment recorded",
                    extra={
                        **self._where(msg),
                        "deployment_id": str(result.deployment_id),
                        "service": event.service,
                        "environment": event.environment,
                        "schema_version": event.schema_version,
                    },
                )
            else:
                self.stats.duplicates += 1
                log.info("duplicate deployment event ignored", extra=self._where(msg))
            return result
        finally:
            reset_log_context(token)

    async def _dead_letter(self, msg: Message, error: EventError, cause: BaseException) -> None:
        outgoing = dead_letter(
            msg,
            dlq_topic=self.dlq_topic,
            error=error,
            cause=cause,
            failed_at=datetime.now(UTC),
        )
        await self._producer.produce(outgoing)
        self.stats.dead_lettered += 1
        log.warning(
            "event dead-lettered",
            extra={**self._where(msg), "error_type": error.error_type, "error": str(error)[:300]},
        )

    async def _commit(self, msg: Message) -> None:
        topic, partition, offset = _position(msg)
        offsets = [TopicPartition(topic, partition, offset + 1)]
        for attempt in range(1, 4):
            try:
                result = await asyncio.to_thread(
                    self._consumer.commit, offsets=offsets, asynchronous=False
                )
                failed = [p for p in (result or []) if p.error is not None]
                if failed:
                    raise KafkaException(failed[0].error)
                self.stats.committed += 1
                return
            except KafkaException as exc:
                code = exc.args[0].code() if exc.args else None
                if code in _REASSIGNED:
                    log.info(
                        "offset not committed: partition reassigned; the new owner re-reads it",
                        extra=self._where(msg),
                    )
                    return
                self.stats.commit_failures += 1
                log.warning(
                    "offset commit failed",
                    extra={**self._where(msg), "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(self._backoff.delay(attempt))
        # Redelivery of an already-persisted event is a no-op, so giving up is safe.
        log.error(
            "offset commit abandoned; the message will be redelivered", extra=self._where(msg)
        )

    # ------------------------------------------------------------------ pause / resume
    def _rewind_and_pause(self, msg: Message) -> None:
        topic, partition, offset = _position(msg)
        # Assignments change only inside poll(), so the partition is still ours here. If
        # pause or seek fails anyway, carrying on could skip this message: the fetch
        # position is already past it. The exception therefore propagates and stops the
        # consumer, and a restart resumes from the committed offset, which precedes it.
        self._consumer.pause([TopicPartition(topic, partition)])
        # Seek back (this also drops prefetched messages), so the same offset comes first
        # after resume.
        self._consumer.seek(TopicPartition(topic, partition, offset))
        delay = self._backoff.pause_delay()
        self._paused[(topic, partition)] = time.monotonic() + delay
        self.stats.pauses += 1
        log.warning(
            "partition paused after repeated transient failures",
            extra={**self._where(msg), "resume_in_s": round(delay, 2)},
        )

    def _resume_due(self) -> None:
        if not self._paused:
            return
        now = time.monotonic()
        due = [tp for tp, at in self._paused.items() if at <= now]
        for topic, partition in due:
            del self._paused[(topic, partition)]
            # A failure here would leave an owned partition paused forever, so it
            # propagates (see _rewind_and_pause).
            self._consumer.resume([TopicPartition(topic, partition)])
            log.info("partition resumed", extra={"topic": topic, "partition": partition})

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _sleep(seconds: float, stop: asyncio.Event) -> bool:
        """Sleep unless stopping. Returns False if ``stop`` was set."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    @staticmethod
    def _where(msg: Message) -> dict[str, Any]:
        return {"topic": msg.topic(), "partition": msg.partition(), "offset": msg.offset()}
