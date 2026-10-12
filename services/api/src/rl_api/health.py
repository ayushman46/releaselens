"""Liveness and readiness probes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])

Check = Callable[[], Awaitable[None]]


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (Postgres + Kafka)")
async def readyz(request: Request) -> JSONResponse:
    checks: dict[str, Check] = request.app.state.readiness_checks
    timeout: float = request.app.state.settings.readiness_timeout_s

    async def run(name: str, check: Check) -> tuple[str, dict[str, Any]]:
        start = time.perf_counter()
        try:
            await asyncio.wait_for(check(), timeout=timeout)
            result: dict[str, Any] = {"status": "ok"}
        except TimeoutError:
            result = {"status": "error", "error": f"timed out after {timeout}s"}
        except Exception as exc:  # readiness must report, never raise
            result = {"status": "error", "error": type(exc).__name__}
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return name, result

    results = dict(await asyncio.gather(*(run(n, c) for n, c in checks.items())))
    ok = all(r["status"] == "ok" for r in results.values())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": results},
        status_code=200 if ok else 503,
    )


def postgres_check(app_state: Any) -> Check:
    async def check() -> None:
        async with app_state.db_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return check


def kafka_check(bootstrap: str, timeout_s: float, producer: Any = None) -> Check:
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(
        {"bootstrap.servers": bootstrap, "socket.timeout.ms": int(timeout_s * 1000)}
    )

    async def check() -> None:
        # A producer hit by a fatal error can never send again, even with the broker up.
        if producer is not None and not producer.healthy:
            raise RuntimeError("kafka producer is in a fatal state")
        # list_topics is blocking; run it off the event loop.
        await asyncio.to_thread(admin.list_topics, timeout=timeout_s)

    return check
