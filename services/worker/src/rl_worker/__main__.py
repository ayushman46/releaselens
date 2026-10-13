"""``python -m rl_worker``: the worker process.

Runs side by side in one asyncio loop:

* the ``deployment.events`` consumer (always);
* the ``telemetry.spans`` consumer (always);
* the analysis job runner with the regression-analysis handlers (early_5m, early_10m, final);
* the outbox relay (unless ``RL_OUTBOX_ENABLED=false``);
* the telemetry maintenance loop (span partitions ahead of time, retention);
* the analysis job runner, only when job handlers are registered (M8 registers them);
* the notifier (M10, unless ``RL_NOTIFIER_ENABLED=false``): the ``analysis.results``
  consumer and the Slack/e-mail dispatcher;
* housekeeping (M10, unless ``RL_HOUSEKEEPING_ENABLED=false``): plan retention and expired
  auth/rate-limit rows;
* a database probe and the health server (``/healthz``, ``/readyz``).

SIGTERM/SIGINT set a stop event. Each component finishes its in-flight unit of work (the
consumer persists and commits its current message) and exits, then the producer is flushed
and connections are closed. If any component crashes, the whole process stops with exit
code 1, so the orchestrator restarts it instead of leaving it running half-broken.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rl_core.obs import configure_logging
from rl_db import create_engine, create_session_factory
from rl_db.repositories import analysis_schedule
from rl_db.secretbox import SecretBox
from rl_worker import __version__
from rl_worker.analysis import AnalysisJobHandler, AnalysisSettings
from rl_worker.consumers.deployments import DeploymentConsumer
from rl_worker.consumers.spans import SpanConsumer
from rl_worker.health import HealthServer
from rl_worker.housekeeping import Maintenance
from rl_worker.jobs import JobRunner, default_registry
from rl_worker.kafka import AsyncProducer
from rl_worker.maintenance import TelemetryMaintenance
from rl_worker.notify.channels import SlackSender, SmtpEmailSender
from rl_worker.notify.consumer import AnalysisResultsConsumer
from rl_worker.notify.dispatcher import NotificationDispatcher
from rl_worker.outbox import OutboxRelay
from rl_worker.retry import Backoff
from rl_worker.settings import WorkerSettings

log = logging.getLogger("rl_worker")


class _DatabaseProbe:
    """Periodic ``SELECT 1`` feeding readiness. Failing it never stops the process."""

    def __init__(self, engine: AsyncEngine, interval_s: float = 5.0) -> None:
        self._engine = engine
        self._interval_s = interval_s
        self.ok = False

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                if not self.ok:
                    log.info("database reachable")
                self.ok = True
            except Exception as exc:
                if self.ok:
                    log.warning("database unreachable", extra={"error": type(exc).__name__})
                self.ok = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)


async def run(settings: WorkerSettings, stop: asyncio.Event) -> int:
    """Run until ``stop`` is set or a component crashes. Returns the process exit code."""
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        connect_timeout_s=settings.db_connect_timeout_s,
        command_timeout_s=settings.db_command_timeout_s,
    )
    sessions = create_session_factory(engine)
    producer = AsyncProducer(
        settings.kafka_bootstrap,
        client_id=f"{settings.service_name}-{settings.worker_id}",
        delivery_timeout_s=settings.kafka_delivery_timeout_s,
    )
    producer.start()
    backoff = Backoff(
        base_s=settings.retry_base_s,
        cap_s=max(settings.retry_cap_s, settings.retry_base_s),
        max_attempts=settings.retry_max_attempts,
    )
    consumer = DeploymentConsumer(
        bootstrap_servers=settings.kafka_bootstrap,
        sessions=sessions,
        producer=producer,
        client_id=f"{settings.service_name}-{settings.worker_id}",
        group_id=settings.deployments_group_id,
        backoff=backoff,
        post_window_end=timedelta(seconds=settings.analysis_post_window_end_s),
        poll_timeout_s=settings.consumer_poll_timeout_s,
    )
    span_consumer = SpanConsumer(
        bootstrap_servers=settings.kafka_bootstrap,
        sessions=sessions,
        producer=producer,
        client_id=f"{settings.service_name}-{settings.worker_id}-spans",
        group_id=settings.spans_group_id,
        backoff=backoff,
        batch_messages=settings.spans_batch_messages,
        poll_timeout_s=settings.consumer_poll_timeout_s,
    )
    maintenance = TelemetryMaintenance(
        sessions,
        interval_s=settings.maintenance_interval_s,
        days_ahead=settings.partition_days_ahead,
        span_retention_days=settings.span_retention_days,
        rollup_retention_days=settings.rollup_retention_days,
    )
    probe = _DatabaseProbe(engine)
    backoff = Backoff(
        base_s=settings.retry_base_s,
        cap_s=max(settings.retry_cap_s, settings.retry_base_s),
        max_attempts=settings.retry_max_attempts,
    )
    notifier: AnalysisResultsConsumer | None = None
    http_client: httpx.AsyncClient | None = None
    if settings.notifier_enabled:
        notifier = AnalysisResultsConsumer(
            bootstrap_servers=settings.kafka_bootstrap,
            sessions=sessions,
            producer=producer,
            client_id=f"{settings.service_name}-{settings.worker_id}-notifier",
            group_id=settings.notifier_group_id,
            backoff=backoff,
            poll_timeout_s=settings.consumer_poll_timeout_s,
        )
        # Slack calls: bounded time, never follow a redirect to wherever it points.
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.slack_timeout_s), follow_redirects=False
        )

    health = HealthServer(settings.health_host, settings.health_port)
    health.register("postgres", lambda: probe.ok)
    health.register("deployments_consumer", lambda: consumer.ready)
    health.register("spans_consumer", lambda: span_consumer.ready)
    health.register("kafka_producer", lambda: producer.healthy)
    if notifier is not None:
        health.register("notifier_consumer", lambda: notifier.ready)
    await health.start()

    components: dict[str, Coroutine[Any, Any, None]] = {
        "database-probe": probe.run(stop),
        "deployments-consumer": consumer.run(stop),
        "spans-consumer": span_consumer.run(stop),
        "telemetry-maintenance": maintenance.run(stop),
    }
    if settings.outbox_enabled:
        relay = OutboxRelay(
            sessions,
            producer,
            batch_size=settings.outbox_batch_size,
            poll_interval_s=settings.outbox_poll_interval_s,
        )
        components["outbox-relay"] = relay.run(stop)
    handler = AnalysisJobHandler(
        sessions,
        AnalysisSettings(
            per_environment=settings.window_profiles(),
            max_spans=settings.analysis_max_spans,
            schedule=analysis_schedule(timedelta(seconds=settings.analysis_post_window_end_s)),
        ),
    )
    registry = default_registry(handler)
    if notifier is not None and http_client is not None:
        components["notifier-consumer"] = notifier.run(stop)
        dispatcher = NotificationDispatcher(
            sessions,
            worker_id=settings.worker_id,
            slack=SlackSender(http_client),
            email=SmtpEmailSender(
                host=settings.smtp_host,
                port=settings.smtp_port,
                sender=settings.mail_from,
                username=settings.smtp_username,
                password=(
                    settings.smtp_password.get_secret_value() if settings.smtp_password else None
                ),
                starttls=settings.smtp_starttls,
                timeout_s=settings.smtp_timeout_s,
            ),
            secret_box=SecretBox(
                settings.master_secret(),
                [s.get_secret_value() for s in settings.secret_key_previous],
            ),
            web_base_url=settings.web_base_url,
            allow_any_slack_host=settings.allow_any_slack_host,
            batch_size=settings.notification_batch_size,
            poll_interval_s=settings.notification_poll_interval_s,
            lease=timedelta(seconds=settings.notification_lease_s),
            backoff=Backoff(
                base_s=settings.notification_retry_base_s,
                cap_s=max(settings.notification_retry_cap_s, settings.notification_retry_base_s),
                max_attempts=6,
            ),
        )
        components["notification-dispatcher"] = dispatcher.run(stop)
    if settings.housekeeping_enabled:
        components["housekeeping"] = Maintenance(
            sessions, interval_s=settings.housekeeping_interval_s
        ).run(stop)
    if len(registry):
        runner = JobRunner(
            sessions,
            registry,
            worker_id=settings.worker_id,
            batch_size=settings.job_batch_size,
            poll_interval_s=settings.job_poll_interval_s,
            lease=timedelta(seconds=settings.job_lease_s),
            job_timeout=timedelta(seconds=settings.job_timeout_s),
        )
        components["job-runner"] = runner.run(stop)
    else:
        log.info("job runner not started: no analysis job handlers are registered")

    tasks = {asyncio.create_task(coro, name=name) for name, coro in components.items()}
    log.info(
        "worker started",
        extra={"version": __version__, "env": settings.env, "components": sorted(components)},
    )
    exit_code = 0
    try:
        stop_wait = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait(tasks | {stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            # A component ended on its own (crash or unexpected return): stop the rest.
            exit_code = 1
            log.error(
                "worker component exited unexpectedly; shutting down",
                extra={"components": sorted(t.get_name() for t in done if t is not stop_wait)},
            )
        stop.set()
        await stop_wait
        _, still_running = await asyncio.wait(tasks, timeout=settings.shutdown_timeout_s)
        for task in still_running:
            log.warning("component did not stop in time", extra={"component": task.get_name()})
            task.cancel()
        if still_running:
            await asyncio.wait(still_running, timeout=5)
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                exit_code = 1
                log.error(
                    "worker component failed",
                    extra={"component": task.get_name()},
                    exc_info=task.exception(),
                )
    finally:
        await health.stop()
        if http_client is not None:
            await http_client.aclose()
        await asyncio.to_thread(producer.close, settings.kafka_delivery_timeout_s)
        await engine.dispose()
        log.info("worker stopped", extra={"exit_code": exit_code})
    return exit_code


def main() -> None:
    settings = WorkerSettings()
    configure_logging(settings.service_name, settings.log_level, settings.log_format)

    async def _main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        return await run(settings, stop)

    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
