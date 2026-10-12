"""``python -m rl_worker`` — worker process entrypoint with graceful shutdown.

M1 skeleton: health server + database connectivity check. Consumers and the job runner are
registered here as they are built (M6+).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from sqlalchemy import text

from rl_core.obs import configure_logging
from rl_db import create_engine
from rl_worker import __version__
from rl_worker.health import HealthServer
from rl_worker.settings import WorkerSettings

log = logging.getLogger("rl_worker")


async def run(settings: WorkerSettings, stop: asyncio.Event) -> None:
    engine = create_engine(settings.database_url, pool_size=2)
    db_ok = False

    health = HealthServer(settings.health_host, settings.health_port)
    health.register("postgres", lambda: db_ok)
    await health.start()
    log.info("worker started", extra={"version": __version__, "env": settings.env})

    try:
        while not stop.is_set():
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                if not db_ok:
                    log.info("database reachable")
                db_ok = True
            except Exception as exc:  # keep running; readiness reflects the state
                if db_ok:
                    log.warning("database unreachable", extra={"error": type(exc).__name__})
                db_ok = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        await health.stop()
        await engine.dispose()
        log.info("worker stopped")


def main() -> None:
    settings = WorkerSettings()
    configure_logging(settings.service_name, settings.log_level, settings.log_format)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
