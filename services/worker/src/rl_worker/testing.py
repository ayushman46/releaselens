"""Test helpers shared by integration tests (not imported by production code)."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

KAFKA_IMAGE = "apache/kafka-native:4.3.1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def kafka_container(image: str = KAFKA_IMAGE) -> Iterator[str]:
    """Start a single-node KRaft broker and yield its host bootstrap address.

    Uses a fixed host port so the advertised listener is correct before the broker starts
    (testcontainers' random port mapping happens after the container's env is set).
    """
    from confluent_kafka.admin import AdminClient
    from testcontainers.core.container import DockerContainer

    port = _free_port()
    container = (
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
    with container:
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
