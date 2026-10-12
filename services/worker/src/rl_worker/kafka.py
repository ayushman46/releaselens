"""Kafka clients for the worker: an awaitable producer and the consumer configuration.

The producer waits for the broker's acknowledgement (``acks=all``, idempotent) before a
caller moves on. The consumer may commit an offset past a dead-lettered message only once
the DLQ copy is acknowledged, and the outbox relay may mark a row published only once the
broker has it.

The API has its own minimal producer (``rl_api.kafka``). The duplication is deliberate: the
API must not import worker code, and a shared infrastructure package would need a new
workspace member.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from confluent_kafka import KafkaError, KafkaException, Message, Producer

log = logging.getLogger("rl_worker.kafka")

_LOOP_GRACE_S = 1.0

Headers = list[tuple[str, bytes]]


class ProduceError(Exception):
    """The broker did not acknowledge the message; treat it as not written."""


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    topic: str
    key: bytes | str | None
    value: bytes | None
    headers: Headers = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Delivery:
    topic: str
    partition: int
    offset: int


def _int_or(value: int | None, default: int) -> int:
    return default if value is None else int(value)


def _settle(fut: asyncio.Future[Delivery], outcome: Delivery | Exception) -> None:
    if fut.done():
        return
    if isinstance(outcome, Exception):
        fut.set_exception(outcome)
    else:
        fut.set_result(outcome)


class AsyncProducer:
    def __init__(
        self,
        bootstrap_servers: str,
        *,
        client_id: str,
        delivery_timeout_s: float,
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        self.delivery_timeout_s = delivery_timeout_s
        config: dict[str, Any] = {
            "bootstrap.servers": bootstrap_servers,
            "client.id": client_id,
            "acks": "all",
            "enable.idempotence": True,
            "message.timeout.ms": int(delivery_timeout_s * 1000),
            "linger.ms": 5,
            "logger": logging.getLogger("rl_worker.kafka.librdkafka"),
            "error_cb": self._on_error,
        }
        config.update(extra_config or {})
        self._producer = Producer(config)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, name="kafka-producer", daemon=True)
        self.fatal_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._producer.poll(0.1)

    def _on_error(self, err: KafkaError) -> None:
        if err.fatal():
            self.fatal_error = err.str()
            log.error("kafka producer fatal error", extra={"error": err.str()})
        else:
            log.debug("kafka client error", extra={"error": err.str()})

    @property
    def healthy(self) -> bool:
        return self.fatal_error is None

    def _enqueue(
        self, loop: asyncio.AbstractEventLoop, msg: OutgoingMessage
    ) -> asyncio.Future[Delivery]:
        fut: asyncio.Future[Delivery] = loop.create_future()

        def on_delivery(err: KafkaError | None, delivered: Message) -> None:
            outcome: Delivery | Exception
            if err is not None:
                outcome = ProduceError(err.str())
            else:
                outcome = Delivery(
                    topic=str(delivered.topic()),
                    partition=_int_or(delivered.partition(), -1),
                    offset=_int_or(delivered.offset(), -1),
                )
            with contextlib.suppress(RuntimeError):  # loop closed during shutdown
                loop.call_soon_threadsafe(_settle, fut, outcome)

        try:
            self._producer.produce(
                msg.topic,
                value=msg.value,
                key=msg.key,
                headers=list(msg.headers),
                on_delivery=on_delivery,
            )
        except BufferError:
            fut.set_exception(ProduceError("local producer queue is full"))
        except KafkaException as exc:
            fut.set_exception(ProduceError(str(exc)))
        return fut

    async def produce(self, msg: OutgoingMessage) -> Delivery:
        """Produce one message and wait for the broker acknowledgement."""
        [outcome] = await self.produce_many([msg])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def produce_many(
        self, messages: Sequence[OutgoingMessage]
    ) -> list[Delivery | ProduceError]:
        """Produce a batch and wait for every acknowledgement. Never raises per message:
        each slot holds a :class:`Delivery` or the :class:`ProduceError` for that message."""
        if not messages:
            return []
        if self.fatal_error is not None:
            return [ProduceError(f"producer unusable: {self.fatal_error}") for _ in messages]
        loop = asyncio.get_running_loop()
        futures = [self._enqueue(loop, m) for m in messages]
        _, pending = await asyncio.wait(futures, timeout=self.delivery_timeout_s + _LOOP_GRACE_S)
        results: list[Delivery | ProduceError] = []
        for fut in futures:
            if fut in pending:
                fut.cancel()
                results.append(ProduceError("timed out waiting for the broker acknowledgement"))
                continue
            exc = fut.exception()
            if exc is None:
                results.append(fut.result())
            else:
                results.append(exc if isinstance(exc, ProduceError) else ProduceError(str(exc)))
        return results

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        remaining = self._producer.flush(timeout_s)
        if remaining:
            log.warning("kafka producer closed with undelivered messages", extra={"n": remaining})


def consumer_config(
    bootstrap_servers: str,
    *,
    group_id: str,
    client_id: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """At-least-once consumer settings (ADR-0007).

    * ``enable.auto.commit=false``: offsets are committed explicitly, and only after the
      Postgres transaction for that message has committed.
    * ``auto.offset.reset=earliest``: a brand-new group starts from the oldest retained
      event, so no deployment is skipped on first start.
    """
    config: dict[str, Any] = {
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "client.id": client_id,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "enable.partition.eof": False,
        # Eager rebalancing (librdkafka's default, pinned here): every rebalance revokes the
        # whole assignment. DeploymentConsumer's rebalance callbacks rely on that.
        "partition.assignment.strategy": "range,roundrobin",
        "logger": logging.getLogger("rl_worker.kafka.librdkafka"),
    }
    config.update(overrides or {})
    return config
