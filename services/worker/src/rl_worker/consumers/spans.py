"""``telemetry.spans`` consumer: batched, at-least-once, idempotent persistence (ADR-0007/0008).

Each poll takes up to ``batch_messages`` messages (from any assigned partitions) and handles
them as one unit:

1. **Parse** every envelope (:func:`rl_core.events.spans.parse_span_batch`). Failures are
   permanent: the message goes to ``telemetry.spans.dlq``.
2. **Tenant check**: envelopes naming a tenant that does not exist are dead-lettered too
   (``spans`` has no foreign key; see :mod:`rl_db.admin.telemetry`).
3. **Partitions**: the day partitions for every span start are created if missing, in a short
   transaction of their own (creating a partition locks the parent table until commit).
4. **One transaction** for the whole batch: per tenant, ``INSERT … ON CONFLICT DO NOTHING
   RETURNING`` and, from the newly inserted spans only, service edges and minute rollups
   (:func:`rl_db.repositories.telemetry.record_spans`).
5. **Offsets** are committed (synchronously) only after that transaction commits.

A crash between 4 and 5 redelivers the batch; step 4 then inserts nothing and adds nothing, so
spans, edge counts and rollups stay exact.

Failure handling, as in :mod:`rl_worker.consumers.deployments`:

* **transient** (database down, timeouts, a DLQ write that was not acknowledged): retry the
  batch with full-jitter backoff; after ``max_attempts``, seek every partition of the batch back
  to its first offset, pause them, and resume after ``pause_delay()``. Nothing is committed and
  nothing is skipped.
* **permanent** during the batch transaction (a value Postgres refuses, say): the batch is
  retried **one message per transaction**, so only the offending message is dead-lettered and
  the rest are stored.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from confluent_kafka import Consumer, KafkaException, Message, TopicPartition
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events import (
    TELEMETRY_SPANS,
    EventError,
    EventValidationError,
    PermanentError,
    TransientError,
    UnknownTenantError,
    dlq,
)
from rl_core.events.spans import SpanBatchEnvelope, parse_span_batch
from rl_core.telemetry.model import Span, ns_to_datetime
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.partitions import day_of, ensure_partitions_for_days
from rl_db.admin.telemetry import existing_tenants
from rl_db.repositories import record_spans
from rl_worker.consumers.deployments import _REASSIGNED
from rl_worker.dlq import dead_letter
from rl_worker.errors import classify
from rl_worker.kafka import AsyncProducer, OutgoingMessage, ProduceError, consumer_config
from rl_worker.retry import Backoff

log = logging.getLogger("rl_worker.consumers.spans")

DEFAULT_GROUP_ID = "rl-spans"
_MISSING_PARTITION_SQLSTATE = "23514"  # check_violation: "no partition of relation … found"


@dataclass
class SpanConsumerStats:
    batches: int = 0
    messages: int = 0  # messages whose offsets were committed
    spans_received: int = 0
    spans_inserted: int = 0
    edge_calls: int = 0
    dead_lettered: int = 0
    transient_errors: int = 0
    pauses: int = 0
    commit_failures: int = 0
    fallbacks: int = 0  # batches retried one message at a time after a permanent error
    partitions_created: int = 0
    busy_s: float = 0.0  # wall time spent handling batches (throughput = inserted / busy_s)

    @property
    def duplicates(self) -> int:
        return self.spans_received - self.spans_inserted


@dataclass
class _Parsed:
    msg: Message
    envelope: SpanBatchEnvelope
    spans: list[Span] = field(default_factory=list)


def _position(msg: Message) -> tuple[str, int, int]:
    topic, partition, offset = msg.topic(), msg.partition(), msg.offset()
    if topic is None or partition is None or offset is None:
        raise ValueError("message has no position")
    return topic, partition, offset


def _is_missing_partition(exc: BaseException) -> bool:
    for link in (exc, getattr(exc, "orig", None), exc.__cause__):
        if link is None:
            continue
        if getattr(link, "sqlstate", None) == _MISSING_PARTITION_SQLSTATE and "partition" in str(
            link
        ):
            return True
    return False


class SpanConsumer:
    def __init__(
        self,
        *,
        bootstrap_servers: str,
        sessions: async_sessionmaker[AsyncSession],
        producer: AsyncProducer,
        client_id: str,
        group_id: str = DEFAULT_GROUP_ID,
        topic: str = TELEMETRY_SPANS,
        backoff: Backoff | None = None,
        batch_messages: int = 100,
        poll_timeout_s: float = 1.0,
        consumer_overrides: dict[str, Any] | None = None,
    ) -> None:
        if batch_messages < 1:
            raise ValueError("batch_messages must be positive")
        self.topic = topic
        self.dlq_topic = dlq(topic)
        self._sessions = sessions
        self._producer = producer
        self._backoff = backoff or Backoff()
        self._batch_messages = batch_messages
        self._poll_timeout_s = poll_timeout_s
        config = consumer_config(
            bootstrap_servers, group_id=group_id, client_id=client_id, overrides=consumer_overrides
        )
        if "cooperative" in str(config.get("partition.assignment.strategy", "")):
            raise ValueError("SpanConsumer requires an eager partition assignment strategy")
        self._consumer = Consumer(config)
        self._paused: dict[tuple[str, int], float] = {}
        self._inflight: asyncio.Future[list[Message]] | None = None
        self._joined = False
        self._known_days: set[date] = set()
        self.stats = SpanConsumerStats()

    # ------------------------------------------------------------------ lifecycle
    @property
    def ready(self) -> bool:
        return self._joined

    def _on_assign(self, consumer: Consumer, partitions: list[TopicPartition]) -> None:
        # Same reasoning as DeploymentConsumer._on_assign: a re-assigned partition must start
        # unpaused, because the resume schedule is dropped on every rebalance.
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
        log.info("partitions revoked", extra={"topic": self.topic})

    def _on_lost(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        self._paused.clear()
        self._joined = False
        log.warning("partitions lost", extra={"topic": self.topic})

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set; the in-flight batch is finished (persisted and
        committed) or left uncommitted for redelivery."""
        self._consumer.subscribe(
            [self.topic],
            on_assign=self._on_assign,
            on_revoke=self._on_revoke,
            on_lost=self._on_lost,
        )
        log.info("span consumer started", extra={"topic": self.topic})
        try:
            while not stop.is_set():
                self._resume_due()
                polled = await self._consume()
                batch: list[Message] = []
                for msg in polled:
                    err = msg.error()
                    if err is None:
                        batch.append(msg)
                    elif err.fatal():
                        raise KafkaException(err)
                    else:
                        log.warning("kafka consumer error", extra={"error": err.str()})
                if batch:
                    await self._handle(batch, stop)
        finally:
            self._joined = False
            try:
                inflight = self._inflight
                if inflight is not None and not inflight.done():
                    await asyncio.wait({inflight}, timeout=self._poll_timeout_s + 10)
                await asyncio.to_thread(self._consumer.close)
            except Exception:
                log.warning("kafka consumer close failed", exc_info=True)
            log.info("span consumer stopped", extra={"stats": vars(self.stats)})

    async def _consume(self) -> list[Message]:
        """``consume()`` in a worker thread, shielded so shutdown can wait for it (see
        DeploymentConsumer._poll for why the handle must not be closed mid-call)."""
        self._inflight = asyncio.ensure_future(
            asyncio.to_thread(self._consumer.consume, self._batch_messages, self._poll_timeout_s)
        )
        return list(await asyncio.shield(self._inflight))

    # ------------------------------------------------------------------ per batch
    async def _handle(self, batch: list[Message], stop: asyncio.Event) -> None:
        started = time.perf_counter()
        dead: set[tuple[str, int, int]] = set()  # dead-lettered during this handling
        attempt = 0
        while True:
            try:
                await self._process(batch, dead)
                break
            except Exception as exc:
                error = classify(exc)
                if isinstance(error, PermanentError):
                    # A permanent failure of the batch transaction: isolate the bad message.
                    self.stats.fallbacks += 1
                    log.warning(
                        "batch failed permanently; retrying one message at a time",
                        extra={"messages": len(batch), "error": str(error)[:300]},
                    )
                    try:
                        await self._process_one_by_one(batch, dead)
                        break
                    except Exception as inner:
                        error = classify(inner)
                        if isinstance(error, PermanentError):  # pragma: no cover - defensive
                            raise
                attempt += 1
                self.stats.transient_errors += 1
                log.warning(
                    "transient failure; will retry the batch",
                    extra={
                        "attempt": attempt,
                        "max_attempts": self._backoff.max_attempts,
                        "messages": len(batch),
                        "error": str(error)[:300],
                    },
                )
                if attempt >= self._backoff.max_attempts:
                    self._rewind_and_pause(batch)
                    return
                if not await self._sleep(self._backoff.delay(attempt), stop):
                    log.info("stopping during retries; batch left for redelivery")
                    return
        await self._commit(batch)
        self.stats.batches += 1
        self.stats.busy_s += time.perf_counter() - started

    def _parse(
        self, batch: Sequence[Message], dead: set[tuple[str, int, int]]
    ) -> tuple[list[_Parsed], list[tuple[Message, EventError, BaseException]]]:
        parsed: list[_Parsed] = []
        bad: list[tuple[Message, EventError, BaseException]] = []
        for msg in batch:
            if _position(msg) in dead:
                continue
            try:
                envelope = parse_span_batch(msg.value())
                spans = envelope.to_spans()
            except PermanentError as exc:
                bad.append((msg, exc, exc))
                continue
            except ValueError as exc:  # a span rl_core refuses (validated twice; defensive)
                bad.append((msg, EventValidationError(str(exc)[:300]), exc))
                continue
            parsed.append(_Parsed(msg=msg, envelope=envelope, spans=spans))
        return parsed, bad

    async def _process(self, batch: Sequence[Message], dead: set[tuple[str, int, int]]) -> None:
        parsed, bad = self._parse(batch, dead)
        async with self._sessions() as session:
            known = await existing_tenants(session, {p.envelope.tenant_id for p in parsed})
        for p in parsed:
            if p.envelope.tenant_id not in known:
                unknown = UnknownTenantError("the batch's tenant does not exist")
                bad.append((p.msg, unknown, unknown))
        parsed = [p for p in parsed if p.envelope.tenant_id in known]
        await self._dead_letter_all(bad, dead)
        if parsed:
            await self._persist(parsed)

    async def _process_one_by_one(
        self, batch: Sequence[Message], dead: set[tuple[str, int, int]]
    ) -> None:
        for msg in batch:
            try:
                await self._process([msg], dead)
            except Exception as exc:
                error = classify(exc)
                if not isinstance(error, PermanentError):
                    raise
                await self._dead_letter_all([(msg, error, exc)], dead)

    async def _ensure_partitions(self, spans: Sequence[Span]) -> None:
        days = {day_of(ns_to_datetime(s.start_ns)) for s in spans} - self._known_days
        if not days:
            return
        async with self._sessions.begin() as session:
            created = await ensure_partitions_for_days(session, days)
        self._known_days |= days
        if created:
            self.stats.partitions_created += len(created)
            log.info("span partitions created", extra={"days": [d.isoformat() for d in created]})

    async def _persist(self, parsed: list[_Parsed]) -> None:
        by_tenant: dict[uuid.UUID, list[Span]] = defaultdict(list)
        for p in parsed:
            by_tenant[p.envelope.tenant_id].extend(p.spans)
        all_spans = [s for spans in by_tenant.values() for s in spans]
        await self._ensure_partitions(all_spans)
        received = inserted = calls = 0
        try:
            async with self._sessions.begin() as session:
                # Sorted: concurrent consumers take the per-tenant locks in one order.
                for tenant in sorted(by_tenant):
                    ctx = TenantContext(tenant_id=tenant, principal=Principal.SYSTEM)
                    result = await record_spans(session, ctx, by_tenant[tenant])
                    received += result.received
                    inserted += result.inserted
                    calls += sum(result.edges.values())
        except DBAPIError as exc:
            if _is_missing_partition(exc):
                # Dropped by retention after we cached it as present: forget and retry.
                self._known_days.clear()
                raise TransientError("span partition missing; recreating") from exc
            raise
        self.stats.spans_received += received
        self.stats.spans_inserted += inserted
        self.stats.edge_calls += calls
        log.debug(
            "span batch stored",
            extra={"messages": len(parsed), "received": received, "inserted": inserted},
        )

    async def _dead_letter_all(
        self,
        bad: Sequence[tuple[Message, EventError, BaseException]],
        dead: set[tuple[str, int, int]],
    ) -> None:
        if not bad:
            return
        now = datetime.now(UTC)
        outgoing: list[OutgoingMessage] = [
            dead_letter(msg, dlq_topic=self.dlq_topic, error=err, cause=cause, failed_at=now)
            for msg, err, cause in bad
        ]
        results = await self._producer.produce_many(outgoing)
        failures = [r for r in results if isinstance(r, ProduceError)]
        for (msg, err, _), result in zip(bad, results, strict=True):
            if not isinstance(result, ProduceError):
                dead.add(_position(msg))
                self.stats.dead_lettered += 1
                log.warning(
                    "span batch dead-lettered",
                    extra={
                        **self._where(msg),
                        "error_type": err.error_type,
                        "error": str(err)[:300],
                    },
                )
        if failures:
            raise TransientError(f"dead-letter publish failed: {failures[0]}")

    async def _commit(self, batch: Sequence[Message]) -> None:
        last: dict[tuple[str, int], int] = {}
        for msg in batch:
            topic, partition, offset = _position(msg)
            last[(topic, partition)] = max(offset, last.get((topic, partition), -1))
        offsets = [TopicPartition(t, p, o + 1) for (t, p), o in sorted(last.items())]
        for attempt in range(1, 4):
            try:
                result = await asyncio.to_thread(
                    self._consumer.commit, offsets=offsets, asynchronous=False
                )
                failed = [p for p in (result or []) if p.error is not None]
                if failed:
                    raise KafkaException(failed[0].error)
                self.stats.messages += len(batch)
                return
            except KafkaException as exc:
                code = exc.args[0].code() if exc.args else None
                if code in _REASSIGNED:
                    log.info("offsets not committed: partitions reassigned; redelivery is a no-op")
                    return
                self.stats.commit_failures += 1
                log.warning("offset commit failed", extra={"attempt": attempt, "error": str(exc)})
                await asyncio.sleep(self._backoff.delay(attempt))
        log.error("offset commit abandoned; the batch will be redelivered (a no-op)")

    # ------------------------------------------------------------------ pause / resume
    def _rewind_and_pause(self, batch: Sequence[Message]) -> None:
        first: dict[tuple[str, int], int] = {}
        for msg in batch:
            topic, partition, offset = _position(msg)
            first[(topic, partition)] = min(offset, first.get((topic, partition), offset))
        delay = self._backoff.pause_delay()
        for (topic, partition), offset in sorted(first.items()):
            self._consumer.pause([TopicPartition(topic, partition)])
            self._consumer.seek(TopicPartition(topic, partition, offset))
            self._paused[(topic, partition)] = time.monotonic() + delay
        self.stats.pauses += 1
        log.warning(
            "partitions paused after repeated transient failures",
            extra={"partitions": sorted(p for _, p in first), "resume_in_s": round(delay, 2)},
        )

    def _resume_due(self) -> None:
        if not self._paused:
            return
        now = time.monotonic()
        for key in [k for k, at in self._paused.items() if at <= now]:
            del self._paused[key]
            self._consumer.resume([TopicPartition(*key)])
            log.info("partition resumed", extra={"topic": key[0], "partition": key[1]})

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _sleep(seconds: float, stop: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    @staticmethod
    def _where(msg: Message) -> dict[str, Any]:
        return {"topic": msg.topic(), "partition": msg.partition(), "offset": msg.offset()}
