"""Topic bootstrap against a real KRaft broker (testcontainers, apache/kafka-native)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from confluent_kafka.admin import AdminClient, ConfigResource

from rl_worker.kafka_admin import TOPICS, bootstrap, dlq

pytestmark = pytest.mark.integration


def test_topic_specs_are_consistent() -> None:
    names = [t.name for t in TOPICS]
    assert len(names) == len(set(names))
    for base in ("deployment.events", "telemetry.spans", "analysis.results"):
        assert base in names
        assert dlq(base) in names


@pytest.fixture(scope="module")
def admin() -> Iterator[AdminClient]:
    from rl_worker.testing import kafka_container

    with kafka_container() as bootstrap_servers:
        yield AdminClient({"bootstrap.servers": bootstrap_servers})


def test_bootstrap_creates_then_is_idempotent(admin: AdminClient) -> None:
    first = bootstrap(admin)
    assert set(first) == {t.name for t in TOPICS}
    assert all(action == "created" for action in first.values())

    md = admin.list_topics(timeout=10).topics
    for spec in TOPICS:
        assert len(md[spec.name].partitions) == spec.partitions

    second = bootstrap(admin)
    assert all(action == "configs_applied" for action in second.values())

    res = ConfigResource(ConfigResource.Type.TOPIC, "telemetry.spans")
    cfg = admin.describe_configs([res])[res].result()
    assert cfg["retention.ms"].value == str(24 * 60 * 60 * 1000)
    assert cfg["cleanup.policy"].value == "delete"
