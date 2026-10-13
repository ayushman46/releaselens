"""``analysis.results`` consumer: validates each envelope with
:func:`rl_core.events.analysis.parse_analysis` and queues its notifications.

It reuses the deployment consumer's hardened loop unchanged (at-least-once, offset committed
after the Postgres commit, DLQ for permanent errors with the ack awaited, retry with
backoff and partition pause for transient ones) and only replaces the per-message step.
Sending happens later, in :mod:`rl_worker.notify.dispatcher`, so a slow Slack never holds
up the partition. Redeliveries are harmless: ``notification_log``'s unique key turns them
into no-ops.
"""

from __future__ import annotations

import logging
from typing import Any

from confluent_kafka import Message

from rl_core.events.analysis import parse_analysis
from rl_core.events.topics import ANALYSIS_RESULTS
from rl_core.obs import bind_log_context, reset_log_context
from rl_db.repositories import RecordedDeployment
from rl_worker.consumers.deployments import DeploymentConsumer
from rl_worker.notify.enqueue import enqueue_for_analysis

log = logging.getLogger("rl_worker.notify.consumer")

DEFAULT_GROUP_ID = "rl-notifier"


class AnalysisResultsConsumer(DeploymentConsumer):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("topic", ANALYSIS_RESULTS)
        kwargs.setdefault("group_id", DEFAULT_GROUP_ID)
        super().__init__(**kwargs)
        self.enqueued = 0

    # The base loop ignores the return value; the signature is kept compatible.
    async def _persist(self, msg: Message) -> RecordedDeployment:
        envelope = parse_analysis(msg.value())  # PermanentError subclasses -> DLQ
        token = bind_log_context(
            tenant_id=str(envelope.tenant_id), deployment_id=str(envelope.payload.deployment_id)
        )
        try:
            async with self._sessions.begin() as session:
                result = await enqueue_for_analysis(session, envelope)
            self.enqueued += result.created
            log.info(
                "analysis result processed",
                extra={
                    **self._where(msg),
                    "kind": envelope.payload.kind,
                    "severity_band": envelope.payload.severity_band,
                    "rules_matched": result.matched_rules,
                    "notifications_queued": result.created,
                },
            )
            return RecordedDeployment(
                created=result.created > 0, deployment_id=None, jobs_scheduled=0
            )
        finally:
            reset_log_context(token)
