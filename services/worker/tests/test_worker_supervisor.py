"""Worker supervision without Docker: a crashing component stops the process with exit 1."""

from __future__ import annotations

import asyncio
import socket

import pytest

from rl_worker import __main__ as worker_main
from rl_worker.consumers.deployments import DeploymentConsumer
from rl_worker.settings import WorkerSettings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _settings() -> WorkerSettings:
    # Unreachable dependencies on purpose: clients connect lazily, so startup still works.
    return WorkerSettings(
        env="test",
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        kafka_bootstrap="127.0.0.1:1",
        health_host="127.0.0.1",
        health_port=_free_port(),
        kafka_delivery_timeout_s=1,
        shutdown_timeout_s=5,
    )


async def test_a_crashing_component_stops_the_worker_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(self: DeploymentConsumer, stop: asyncio.Event) -> None:
        raise RuntimeError("consumer blew up")

    monkeypatch.setattr(DeploymentConsumer, "run", explode)
    stop = asyncio.Event()
    assert await asyncio.wait_for(worker_main.run(_settings(), stop), timeout=20) == 1
    assert stop.is_set()  # the other components were told to stop


async def test_stop_event_gives_exit_code_0(monkeypatch: pytest.MonkeyPatch) -> None:
    async def idle(self: DeploymentConsumer, stop: asyncio.Event) -> None:
        await stop.wait()

    monkeypatch.setattr(DeploymentConsumer, "run", idle)
    stop = asyncio.Event()
    task = asyncio.create_task(worker_main.run(_settings(), stop))
    await asyncio.sleep(0.5)
    stop.set()
    assert await asyncio.wait_for(task, timeout=20) == 0
