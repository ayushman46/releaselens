"""Kafka topic bootstrap (ADR-0007). Single source of truth for topic names and settings.

``python -m rl_worker.kafka_admin bootstrap [--bootstrap-servers kafka:9092]``

Idempotent: missing topics are created; existing topics get their retention/cleanup configs
re-applied (partition counts are only ever increased, never decreased).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass

from confluent_kafka import KafkaException
from confluent_kafka.admin import (
    AdminClient,
    AlterConfigOpType,
    ConfigEntry,
    ConfigResource,
    NewPartitions,
    NewTopic,
)

log = logging.getLogger("rl_worker.kafka_admin")

_DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class TopicSpec:
    name: str
    partitions: int
    retention_ms: int

    def configs(self) -> dict[str, str]:
        return {
            "retention.ms": str(self.retention_ms),
            "cleanup.policy": "delete",
            "min.insync.replicas": "1",
        }


DEPLOYMENT_EVENTS = "deployment.events"
TELEMETRY_SPANS = "telemetry.spans"
ANALYSIS_RESULTS = "analysis.results"


def dlq(topic: str) -> str:
    return f"{topic}.dlq"


TOPICS: tuple[TopicSpec, ...] = (
    TopicSpec(DEPLOYMENT_EVENTS, 6, 7 * _DAY_MS),
    TopicSpec(TELEMETRY_SPANS, 12, 1 * _DAY_MS),
    TopicSpec(ANALYSIS_RESULTS, 6, 7 * _DAY_MS),
    TopicSpec(dlq(DEPLOYMENT_EVENTS), 1, 30 * _DAY_MS),
    TopicSpec(dlq(TELEMETRY_SPANS), 1, 30 * _DAY_MS),
    TopicSpec(dlq(ANALYSIS_RESULTS), 1, 30 * _DAY_MS),
)


def wait_for_broker(admin: AdminClient, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            admin.list_topics(timeout=5)
            return
        except KafkaException as exc:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Kafka not reachable after {timeout_s}s") from exc
            log.info("waiting for broker")
            time.sleep(2)


def bootstrap(admin: AdminClient, topics: tuple[TopicSpec, ...] = TOPICS) -> dict[str, str]:
    """Ensure every topic exists with its configs. Returns {topic: action}."""
    existing = admin.list_topics(timeout=10).topics
    actions: dict[str, str] = {}

    to_create = [
        NewTopic(t.name, num_partitions=t.partitions, replication_factor=1, config=t.configs())
        for t in topics
        if t.name not in existing
    ]
    if to_create:
        for name, fut in admin.create_topics(to_create, operation_timeout=30).items():
            fut.result()
            actions[name] = "created"

    grow = [
        NewPartitions(t.name, t.partitions)
        for t in topics
        if t.name in existing and len(existing[t.name].partitions) < t.partitions
    ]
    if grow:
        for name, fut in admin.create_partitions(grow, operation_timeout=30).items():
            fut.result()
            actions[name] = "partitions_increased"

    resources = [
        ConfigResource(
            ConfigResource.Type.TOPIC,
            t.name,
            incremental_configs=[
                ConfigEntry(k, v, incremental_operation=AlterConfigOpType.SET)
                for k, v in t.configs().items()
            ],
        )
        for t in topics
        if t.name in existing
    ]
    if resources:
        for res, fut in admin.incremental_alter_configs(resources).items():
            fut.result()
            actions.setdefault(res.name, "configs_applied")
    return actions


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="rl_worker.kafka_admin")
    parser.add_argument("command", choices=["bootstrap", "list"])
    parser.add_argument(
        "--bootstrap-servers", default=os.environ.get("RL_KAFKA_BOOTSTRAP", "localhost:29092")
    )
    args = parser.parse_args(argv)

    admin = AdminClient({"bootstrap.servers": args.bootstrap_servers})
    wait_for_broker(admin)
    if args.command == "bootstrap":
        for topic, action in sorted(bootstrap(admin).items()):
            log.info("%s: %s", topic, action)
    md = admin.list_topics(timeout=10).topics
    for spec in TOPICS:
        parts = len(md[spec.name].partitions) if spec.name in md else 0
        log.info("topic %s partitions=%d", spec.name, parts)
    missing = [t.name for t in TOPICS if t.name not in md]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
