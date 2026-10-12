"""The worker entrypoint and the full pipeline: API → Kafka → worker → Postgres → API."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.apikeys import create_api_key
from rl_api.app import create_app
from rl_api.settings import Settings as ApiSettings
from rl_core.events import DEPLOYMENT_EVENTS
from rl_db.models import Deployment
from rl_worker.__main__ import run
from rl_worker.settings import WorkerSettings
from rl_worker.testing import committed_vs_end, make_event, produce_raw, wait_until

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _http_status(port: int, path: str) -> int:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return 0
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readline()
    writer.close()
    return int(head.split()[1]) if head else 0


def _settings(db_url: str, bootstrap: str, group: str, port: int) -> WorkerSettings:
    return WorkerSettings(
        env="test",
        database_url=db_url,
        kafka_bootstrap=bootstrap,
        deployments_group_id=group,
        health_host="127.0.0.1",
        health_port=port,
        consumer_poll_timeout_s=0.2,
        shutdown_timeout_s=15,
    )


async def test_worker_runs_until_stopped_and_shuts_down_cleanly(
    worker_db_url: str, kafka_bootstrap: str, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    group = f"worker-main-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    stop = asyncio.Event()
    started = time.perf_counter()
    task = asyncio.create_task(run(_settings(worker_db_url, kafka_bootstrap, group, port), stop))

    await wait_until(lambda: _http_status(port, "/readyz"), timeout_s=30)
    await wait_until(lambda: _ready(port), timeout_s=30)
    print(f"[measure] worker_start_to_ready={time.perf_counter() - started:.3f}s", file=sys.stderr)

    event_id, raw = make_event(tenant_id, service="main-svc")
    produce_raw(kafka_bootstrap, DEPLOYMENT_EVENTS, [(f"{tenant_id}:main-svc", raw)])

    async def persisted() -> bool:
        async with sessions() as s:
            n = await s.scalar(
                select(func.count()).select_from(Deployment).where(Deployment.event_id == event_id)
            )
        return n == 1

    await wait_until(persisted, timeout_s=30)

    stopping = time.perf_counter()
    stop.set()  # what the SIGTERM handler does
    assert await asyncio.wait_for(task, timeout=30) == 0
    print(
        f"[measure] worker_graceful_shutdown={time.perf_counter() - stopping:.3f}s", file=sys.stderr
    )
    assert await _http_status(port, "/healthz") == 0  # health server closed
    offsets = committed_vs_end(kafka_bootstrap, group, DEPLOYMENT_EVENTS)
    assert all(committed == end for committed, end in offsets.values() if end)


async def _ready(port: int) -> bool:
    return await _http_status(port, "/readyz") == 200


async def test_api_to_worker_end_to_end(
    worker_db_url: str, kafka_bootstrap: str, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    async with sessions.begin() as s:
        key = await create_api_key(s, tenant_id, "e2e", ["deployments:write", "read"])
    api = TestClient(
        create_app(
            ApiSettings(
                env="test",
                database_url=worker_db_url,
                kafka_bootstrap=kafka_bootstrap,
                otel_enabled=False,
            )
        )
    )
    headers = {"Authorization": f"Bearer {key.plaintext}"}
    group = f"worker-e2e-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    stop = asyncio.Event()
    worker = asyncio.create_task(run(_settings(worker_db_url, kafka_bootstrap, group, port), stop))
    try:
        with api:
            deployed_at = datetime.now(UTC) - timedelta(seconds=30)
            body = {
                "service": "checkout",
                "environment": "staging",
                "version": "2026.10.13-1",
                "commit_sha": "abcdef0123456",
                "deployed_at": deployed_at.isoformat(),
                "labels": {"team": "web"},
            }
            started = time.perf_counter()
            # TestClient blocks this event loop while the API runs in its own thread;
            # the worker simply catches up afterwards.
            accepted = api.post("/api/v1/deployments", json=body, headers=headers)
            assert accepted.status_code == 202, accepted.text
            event_id = uuid.UUID(accepted.json()["event_id"])

            async def visible() -> bool:
                async with sessions() as s:
                    return bool(
                        await s.scalar(select(Deployment.id).where(Deployment.event_id == event_id))
                    )

            await wait_until(visible, timeout_s=30)
            print(
                f"[measure] api_202_to_row_visible={time.perf_counter() - started:.3f}s",
                file=sys.stderr,
            )
            listing = api.get(
                "/api/v1/deployments", params={"service": "checkout"}, headers=headers
            )
            [item] = listing.json()["items"]
            assert item["event_id"] == str(event_id)
            assert item["labels"] == {"team": "web"}
            detail = api.get(f"/api/v1/deployments/{item['id']}", headers=headers).json()
            assert sorted(j["kind"] for j in detail["analysis_jobs"]) == [
                "early_10m",
                "early_5m",
                "final",
            ]
            assert all(j["status"] == "pending" for j in detail["analysis_jobs"])
    finally:
        stop.set()
        assert await asyncio.wait_for(worker, timeout=30) == 0


async def test_sigterm_on_the_real_process_finishes_in_flight_work_and_exits_0(
    worker_db_url: str, kafka_bootstrap: str, sessions: Sessions, tenant_id: uuid.UUID
) -> None:
    port = _free_port()
    group = f"worker-sigterm-{uuid.uuid4().hex[:8]}"
    env = {
        **os.environ,
        "RL_ENV": "test",
        "RL_LOG_FORMAT": "json",
        "RL_DATABASE_URL": worker_db_url,
        "RL_KAFKA_BOOTSTRAP": kafka_bootstrap,
        "RL_DEPLOYMENTS_GROUP_ID": group,
        "RL_HEALTH_HOST": "127.0.0.1",
        "RL_HEALTH_PORT": str(port),
        "RL_CONSUMER_POLL_TIMEOUT_S": "0.2",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "rl_worker",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await wait_until(lambda: _ready(port), timeout_s=30)
        event_id, raw = make_event(tenant_id, service="sigterm-svc")
        produce_raw(kafka_bootstrap, DEPLOYMENT_EVENTS, [(f"{tenant_id}:sigterm-svc", raw)])

        async def persisted() -> bool:
            async with sessions() as s:
                return bool(
                    await s.scalar(select(Deployment.id).where(Deployment.event_id == event_id))
                )

        await wait_until(persisted, timeout_s=30)
        sent = time.perf_counter()
        proc.send_signal(signal.SIGTERM)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        print(f"[measure] sigterm_to_exit={time.perf_counter() - sent:.3f}s", file=sys.stderr)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode == 0
    logs = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith("{")]
    messages = [entry["msg"] for entry in logs]
    assert "deployment consumer stopped" in messages
    assert {"msg": "worker stopped", "exit_code": 0}.items() <= next(
        e for e in logs if e["msg"] == "worker stopped"
    ).items()
    started = next(e for e in logs if e["msg"] == "worker started")
    assert {"job-runner", "spans-consumer", "telemetry-maintenance"} <= set(started["components"])
    assert any(e["msg"] == "span consumer stopped" for e in logs)
    offsets = committed_vs_end(kafka_bootstrap, group, DEPLOYMENT_EVENTS)
    assert all(committed == end for committed, end in offsets.values() if end)
