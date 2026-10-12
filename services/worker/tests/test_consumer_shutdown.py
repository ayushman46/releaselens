"""DeploymentConsumer lifecycle guards that need no broker: shutdown ordering, config.

librdkafka forbids destroying a handle while another thread is still inside ``poll()`` on
it. ``poll()`` runs in a worker thread that task cancellation cannot interrupt, so the
consumer must wait for that call to return before ``close()``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from rl_worker.consumers.deployments import DeploymentConsumer


class _SlowConsumer:
    """Stands in for confluent_kafka.Consumer and records when poll and close run."""

    def __init__(self) -> None:
        self.events: list[tuple[str, float]] = []
        self.polling = threading.Event()

    def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def poll(self, _timeout: float) -> None:
        self.polling.set()
        time.sleep(0.4)
        self.events.append(("poll_returned", time.monotonic()))

    def close(self) -> None:
        self.events.append(("close", time.monotonic()))


async def test_cancellation_never_closes_the_client_while_poll_is_running() -> None:
    consumer = DeploymentConsumer(
        bootstrap_servers="127.0.0.1:1",
        sessions=None,  # type: ignore[arg-type]  # never reached: no message arrives
        producer=None,  # type: ignore[arg-type]
        client_id="test",
        poll_timeout_s=0.4,
    )
    real = consumer._consumer
    fake = _SlowConsumer()
    consumer._consumer = fake  # type: ignore[assignment]
    try:
        task = asyncio.create_task(consumer.run(asyncio.Event()))
        await asyncio.to_thread(fake.polling.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        real.close()

    names = [name for name, _ in fake.events]
    assert names == ["poll_returned", "close"], names


def test_cooperative_rebalancing_is_refused() -> None:
    """The pause bookkeeping assumes eager rebalancing; a cooperative override would leave
    partitions paused or double-assigned, so it is rejected up front."""
    with pytest.raises(ValueError, match="eager"):
        DeploymentConsumer(
            bootstrap_servers="127.0.0.1:1",
            sessions=None,  # type: ignore[arg-type]
            producer=None,  # type: ignore[arg-type]
            client_id="test",
            consumer_overrides={"partition.assignment.strategy": "cooperative-sticky"},
        )
