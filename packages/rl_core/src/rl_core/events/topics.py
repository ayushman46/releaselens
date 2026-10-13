"""Kafka topic names and message keys (ADR-0007).

Topic *names* live here, in the I/O-free core, so the API (producer) and the worker
(consumers, topic bootstrap) agree on them without the API importing worker code.
Partition counts, retention and cleanup policy stay with the bootstrap code in
``rl_worker.kafka_admin``, the only place that creates topics.
"""

from __future__ import annotations

import uuid

DEPLOYMENT_EVENTS = "deployment.events"
TELEMETRY_SPANS = "telemetry.spans"
ANALYSIS_RESULTS = "analysis.results"


def dlq(topic: str) -> str:
    """Name of the dead-letter topic paired with ``topic``."""
    return f"{topic}.dlq"


DEPLOYMENT_EVENTS_DLQ = dlq(DEPLOYMENT_EVENTS)
TELEMETRY_SPANS_DLQ = dlq(TELEMETRY_SPANS)


def deployment_key(tenant_id: uuid.UUID, service: str) -> str:
    """Message key for ``deployment.events``: ``"<tenant_id>:<service>"``.

    Keying by tenant *and* service keeps every deployment of one service in one partition,
    so a consumer sees them in the order they were produced. The tenant id comes first and
    contains no ``:``, so the key splits unambiguously on the first ``:``.
    """
    return f"{tenant_id}:{service}"


def span_key(tenant_id: uuid.UUID, service: str) -> str:
    """Message key for ``telemetry.spans``: ``"<tenant_id>:<service.name>"`` (ADR-0007).

    One service's spans stay in one partition, so a consumer sees each service's batches in
    the order the API produced them.
    """
    return f"{tenant_id}:{service}"
