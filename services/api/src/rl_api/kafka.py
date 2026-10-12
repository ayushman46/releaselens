"""Async-friendly Kafka producer that waits for the broker's acknowledgement.

The API answers ``202 Accepted`` only after the broker has acknowledged the event with
``acks=all``. Until then the event exists only in this process's memory, and a crash would
lose it while the client believes it was recorded. ``enable.idempotence`` stops
librdkafka's internal retries from writing duplicates. Retries by the *client* are
deduplicated downstream on ``(tenant_id, event_id)``.

librdkafka calls delivery callbacks from whichever thread calls ``poll()``. A dedicated
daemon thread does that, and each callback resolves an asyncio future on the event loop
through ``call_soon_threadsafe``.

``message.timeout.ms`` is the delivery deadline inside librdkafka, and the asyncio wait
allows a little more. When the broker is unreachable, librdkafka itself gives up first
and the API answers 503 knowing the message will *not* be delivered later. If the asyncio
timeout fired first, a 503 could be followed by a late delivery. That is still safe, since
the client retries with the same ``event_id``, but it is avoidable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from confluent_kafka import KafkaError, KafkaException, Message, Producer

log = logging.getLogger("rl_api.kafka")

_LOOP_GRACE_S = 1.0


class ProduceError(Exception):
    """The event was not acknowledged by the broker and must be treated as not written."""


@dataclass(frozen=True, slots=True)
class Delivery:
    topic: str
    partition: int
    offset: int


def _int_or(value: int | None, default: int) -> int:
    return default if value is None else int(value)


def _settle(fut: asyncio.Future[Delivery], outcome: Delivery | Exception) -> None:
    if fut.done():  # the waiter timed out or was cancelled
        return
    if isinstance(outcome, Exception):
        fut.set_exception(outcome)
    else:
        fut.set_result(outcome)


class EventProducer:
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
            # Span batches are JSON and compress several-fold; deployment events barely notice.
            "compression.type": "lz4",
            "logger": logging.getLogger("rl_api.kafka.librdkafka"),
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
        # Fatal errors (e.g. idempotence violated) make the producer permanently unusable;
        # readiness reports it so the orchestrator replaces the process.
        if err.fatal():
            self.fatal_error = err.str()
            log.error("kafka producer fatal error", extra={"error": err.str()})
        else:
            log.debug("kafka client error", extra={"error": err.str()})

    @property
    def healthy(self) -> bool:
        return self.fatal_error is None

    async def produce(
        self,
        topic: str,
        *,
        key: str,
        value: bytes,
        headers: Sequence[tuple[str, bytes]] = (),
    ) -> Delivery:
        """Produce one message and wait until the broker acknowledges it."""
        if self.fatal_error is not None:
            raise ProduceError(f"producer unusable: {self.fatal_error}")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Delivery] = loop.create_future()

        def on_delivery(err: KafkaError | None, msg: Message) -> None:
            outcome: Delivery | Exception
            if err is not None:
                outcome = ProduceError(err.str())
            else:
                outcome = Delivery(
                    topic=str(msg.topic()),
                    partition=_int_or(msg.partition(), -1),
                    offset=_int_or(msg.offset(), -1),
                )
            # RuntimeError: the event loop already closed during shutdown.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_settle, fut, outcome)

        try:
            self._producer.produce(
                topic, value=value, key=key, headers=list(headers), on_delivery=on_delivery
            )
        except BufferError as exc:
            raise ProduceError("local producer queue is full") from exc
        except KafkaException as exc:
            raise ProduceError(str(exc)) from exc
        try:
            return await asyncio.wait_for(fut, timeout=self.delivery_timeout_s + _LOOP_GRACE_S)
        except TimeoutError as exc:
            raise ProduceError("timed out waiting for the broker acknowledgement") from exc

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop polling and flush what is still queued (bounded by ``timeout_s``)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        remaining = self._producer.flush(timeout_s)
        if remaining:
            log.warning("kafka producer closed with undelivered messages", extra={"n": remaining})
