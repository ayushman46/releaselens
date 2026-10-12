"""Test helpers shared by integration tests (not imported by production code)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from confluent_kafka import OFFSET_BEGINNING, Consumer, Message, Producer, TopicPartition

from rl_core.events import (
    DeploymentPayloadV1,
    DeploymentPayloadV2,
    encode_envelope,
    make_envelope,
)

KAFKA_IMAGE = "apache/kafka-native:4.3.1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _kafka_broker(image: str, port: int) -> Any:
    from testcontainers.core.container import DockerContainer

    return (
        DockerContainer(image)
        .with_bind_ports(9092, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", "PLAINTEXT://:9092,CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"PLAINTEXT://127.0.0.1:{port}")
        .with_env(
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT"
        )
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
        .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
        .with_env("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false")
    )


@contextmanager
def kafka_container(image: str = KAFKA_IMAGE, *, port_attempts: int = 3) -> Iterator[str]:
    """Start a single-node KRaft broker and yield its host bootstrap address.

    Uses a fixed host port so the advertised listener is correct before the broker starts
    (testcontainers' random port mapping happens after the container's env is set). A port
    that was free on this host can still be taken inside the Docker VM by the time the
    container starts (measured: "failed to bind host port ...: address already in use"),
    so that one error is retried on a fresh port. A container that failed to start is
    removed, never left behind in the shared VM.
    """
    from confluent_kafka.admin import AdminClient
    from docker.errors import APIError

    for attempt in range(1, port_attempts + 1):
        port = _free_port()
        container = _kafka_broker(image, port)
        try:
            container.start()
        except APIError as exc:
            with contextlib.suppress(Exception):
                container.stop()
            if "address already in use" not in str(exc) or attempt == port_attempts:
                raise
            continue
        break

    try:
        bootstrap = f"127.0.0.1:{port}"
        admin = AdminClient({"bootstrap.servers": bootstrap})
        deadline = time.monotonic() + 60
        while True:
            try:
                admin.list_topics(timeout=2)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield bootstrap
    finally:
        container.stop()


class TcpProxy:
    """A local TCP proxy that can simulate an outage of whatever is behind it.

    Database-outage tests point the worker at the proxy. :meth:`cut` then aborts every open
    connection and stops listening, so new connections are refused, as when Postgres
    restarts or the network drops. :meth:`restore` listens again on the same port.
    This is deterministic and touches no container, unlike ``docker pause``.
    """

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.connections_accepted = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.port, reuse_address=True
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        self.connections_accepted += 1
        try:
            upstream_r, upstream_w = await asyncio.open_connection(
                self.target_host, self.target_port
            )
        except OSError:
            client_w.close()
            return
        self._writers.update({client_w, upstream_w})
        try:
            await asyncio.gather(self._pipe(client_r, upstream_w), self._pipe(upstream_r, client_w))
        finally:
            self._writers.difference_update({client_w, upstream_w})

    async def cut(self) -> None:
        """Refuse new connections and abort the open ones."""
        server, self._server = self._server, None
        if server is not None:
            server.close()  # stop accepting first
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()
        if server is not None:
            # Since Python 3.12, wait_closed() also waits for the handlers' connections,
            # which the aborts above have just ended.
            await asyncio.wait_for(server.wait_closed(), timeout=5)

    async def restore(self) -> None:
        await self.start()

    async def close(self) -> None:
        await self.cut()


# ----------------------------------------------------------------------------- Kafka/event helpers
def make_payload(version: int = 2, **overrides: Any) -> DeploymentPayloadV1:
    """A valid payload (v1 or v2); ``overrides`` replace or add fields."""
    fields: dict[str, Any] = {
        "service": "payment",
        "environment": "production",
        "version": "v2",
        "previous_version": "v1",
        "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "deployed_at": datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    model = DeploymentPayloadV2 if version == 2 else DeploymentPayloadV1
    return model.model_validate(fields)


def make_event(tenant: uuid.UUID, version: int = 2, **overrides: Any) -> tuple[uuid.UUID, bytes]:
    """``(event_id, wire bytes)`` of a valid envelope. Pass ``event_id=`` to fix the id."""
    event_id = overrides.pop("event_id", None)
    env = make_envelope(
        tenant_id=tenant, payload=make_payload(version, **overrides), event_id=event_id
    )
    return env.event_id, encode_envelope(env)


def produce_raw(bootstrap: str, topic: str, messages: list[tuple[str | None, bytes]]) -> None:
    """Produce arbitrary bytes (including invalid events) and wait for delivery."""
    producer = Producer({"bootstrap.servers": bootstrap, "acks": "all"})
    for key, value in messages:
        producer.produce(topic, key=key, value=value)
    if producer.flush(15) != 0:
        raise AssertionError("test messages not delivered")


def _plain_consumer(bootstrap: str, group: str) -> Consumer:
    return Consumer(
        {"bootstrap.servers": bootstrap, "group.id": group, "enable.auto.commit": False}
    )


def read_topic(bootstrap: str, topic: str, expect: int, timeout_s: float = 20.0) -> list[Message]:
    """Read from the beginning until ``expect`` messages arrived (or time out), then linger
    one more second so that *unexpected extra* messages are caught too."""
    consumer = _plain_consumer(bootstrap, f"reader-{uuid.uuid4()}")
    try:
        md = consumer.list_topics(topic, timeout=10)
        consumer.assign(
            [TopicPartition(topic, p, OFFSET_BEGINNING) for p in md.topics[topic].partitions]
        )
        out: list[Message] = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and len(out) < expect:
            msg = consumer.poll(0.3)
            if msg is not None and msg.error() is None:
                out.append(msg)
        linger = time.monotonic() + 1.0
        while time.monotonic() < linger:
            msg = consumer.poll(0.2)
            if msg is not None and msg.error() is None:
                out.append(msg)
        return out
    finally:
        consumer.close()


def committed_vs_end(bootstrap: str, group: str, topic: str) -> dict[int, tuple[int, int]]:
    """``{partition: (committed offset, high watermark)}``. A partition is fully processed
    when the two are equal. An offset below 0 means nothing was committed."""
    consumer = _plain_consumer(bootstrap, group)
    try:
        md = consumer.list_topics(topic, timeout=10)
        parts = [TopicPartition(topic, p) for p in md.topics[topic].partitions]
        result: dict[int, tuple[int, int]] = {}
        for tp in consumer.committed(parts, timeout=10):
            _, high = consumer.get_watermark_offsets(
                TopicPartition(topic, tp.partition), timeout=10
            )
            result[tp.partition] = (tp.offset, high)
        return result
    finally:
        consumer.close()


async def wait_until(
    predicate: Callable[[], Any], timeout_s: float = 30.0, step_s: float = 0.1
) -> None:
    """Poll ``predicate`` (sync or async) until truthy; fail the test after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step_s)


def headers_of(msg: Message) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for header in msg.headers() or []:
        value = header[1]
        out[str(header[0])] = value if isinstance(value, bytes) else str(value or "").encode()
    return out


def value_json(msg: Message) -> dict[str, Any]:
    return dict(json.loads(msg.value() or b"{}"))
