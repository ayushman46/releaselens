"""Notification dispatcher: delivers queued ``notification_log`` rows over Slack and e-mail.

Each round claims a batch (``FOR UPDATE SKIP LOCKED`` with a lease, so several workers
share the queue without double sends), then delivers each row in its own TenantContext:

1. load the rule; deleted or disabled rule → ``skipped``;
2. Slack only: the tenant's plan must include Slack alerts (a downgrade applies to rows
   already queued) → else ``skipped``; the stored ciphertext is decrypted (AES-GCM bound to
   the rule's row) and the URL is validated again (``hooks.slack.com`` only outside
   dev/test) before any request;
3. send; success → ``sent``; transient failure → ``failed`` with full-jitter exponential
   backoff (or Slack's ``Retry-After``); permanent failure or attempts used up → ``dead``.

``dead`` rows are the notifier's dead-letter queue: they keep the payload and the last
error, and are listed by ``GET /api/v1/orgs/{org_id}/notifications?status=dead``. Poison
*events* (unparseable ``analysis.results`` messages) go to the Kafka DLQ
``analysis.results.dlq`` through the consumer instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.alerting import validate_slack_webhook_url
from rl_core.plans import limits_for
from rl_db.admin.notifications import (
    ClaimedNotification,
    claim_due_notifications,
    mark_failed,
    mark_sent,
    mark_skipped,
    reap_expired_leases,
    release_notification,
)
from rl_db.models import NotificationChannel
from rl_db.repositories.alerts import get_alert_rule_by_id
from rl_db.repositories.orgs import get_organization
from rl_db.secretbox import SecretBox, SecretBoxError, slack_webhook_context
from rl_worker.notify import render
from rl_worker.notify.channels import DeliveryError, EmailSender, SlackSender
from rl_worker.retry import Backoff

log = logging.getLogger("rl_worker.notify.dispatcher")


@dataclass
class DispatchStats:
    sent: int = 0
    failed: int = 0
    dead: int = 0
    skipped: int = 0


class SkipNotificationError(Exception):
    """Control flow: do not send this row, mark it skipped with the message as reason."""


class NotificationDispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        slack: SlackSender,
        email: EmailSender,
        secret_box: SecretBox,
        web_base_url: str,
        allow_any_slack_host: bool,
        batch_size: int = 20,
        poll_interval_s: float = 1.0,
        lease: timedelta = timedelta(minutes=5),
        backoff: Backoff | None = None,
    ) -> None:
        self._sessions = sessions
        self._worker_id = worker_id
        self._slack = slack
        self._email = email
        self._box = secret_box
        self._web_base_url = web_base_url
        self._allow_any_slack_host = allow_any_slack_host
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._lease = lease
        self._backoff = backoff or Backoff(base_s=30.0, cap_s=1800.0, max_attempts=6)
        self.stats = DispatchStats()

    async def run(self, stop: asyncio.Event) -> None:
        log.info("notification dispatcher started")
        while not stop.is_set():
            try:
                handled = await self.run_once(stop)
            except Exception:
                log.warning("dispatch round failed; retrying", exc_info=True)
                handled = 0
            if handled == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_s)
        log.info("notification dispatcher stopped", extra={"stats": vars(self.stats)})

    async def run_once(self, stop: asyncio.Event | None = None) -> int:
        async with self._sessions.begin() as session:
            await reap_expired_leases(session, lease=self._lease)
            batch = await claim_due_notifications(
                session, worker_id=self._worker_id, limit=self._batch_size, lease=self._lease
            )
        for index, row in enumerate(batch):
            if stop is not None and stop.is_set():
                await self._release(batch[index:])
                break
            try:
                await self._deliver(row)
            except asyncio.CancelledError:
                await asyncio.shield(self._release(batch[index:]))
                raise
        return len(batch)

    async def _release(self, rows: list[ClaimedNotification]) -> None:
        async with self._sessions.begin() as session:
            for row in rows:
                await release_notification(session, row.id, self._worker_id)

    async def _deliver(self, row: ClaimedNotification) -> None:
        try:
            await self._send(row)
        except SkipNotificationError as skip:
            async with self._sessions.begin() as session:
                await mark_skipped(session, row.id, self._worker_id, reason=str(skip))
            self.stats.skipped += 1
            log.info("notification skipped", extra={"id": str(row.id), "reason": str(skip)})
            return
        except DeliveryError as exc:
            await self._failed(
                row, str(exc), permanent=exc.permanent, retry_after=exc.retry_after_s
            )
            return
        except Exception as exc:  # an unclassified bug: retry, it may be a blip
            log.exception("notification delivery crashed", extra={"id": str(row.id)})
            await self._failed(
                row, f"{type(exc).__name__}: {exc}", permanent=False, retry_after=None
            )
            return
        async with self._sessions.begin() as session:
            await mark_sent(session, row.id, self._worker_id)
        self.stats.sent += 1
        log.info(
            "notification sent",
            extra={
                "id": str(row.id),
                "channel": row.channel,
                "kind": row.kind,
                "tenant_id": str(row.tenant_id),
            },
        )

    async def _failed(
        self, row: ClaimedNotification, error: str, *, permanent: bool, retry_after: float | None
    ) -> None:
        delay = max(
            1.0, retry_after if retry_after is not None else self._backoff.delay(row.attempts)
        )
        async with self._sessions.begin() as session:
            status = await mark_failed(
                session,
                row.id,
                self._worker_id,
                error=error,
                retry_delay=timedelta(seconds=delay),
                permanent=permanent,
            )
        if status == "dead":
            self.stats.dead += 1
            log.error(
                "notification dead-lettered",
                extra={
                    "id": str(row.id),
                    "channel": row.channel,
                    "attempts": row.attempts,
                    "error": error[:300],
                },
            )
        else:
            self.stats.failed += 1
            log.warning(
                "notification failed; will retry",
                extra={"id": str(row.id), "attempts": row.attempts, "retry_in_s": round(delay, 1)},
            )

    async def _send(self, row: ClaimedNotification) -> None:
        ctx = row.tenant_context()
        if row.alert_rule_id is None:
            raise SkipNotificationError("alert rule was deleted")
        async with self._sessions() as session:
            rule = await get_alert_rule_by_id(session, ctx, row.alert_rule_id)
            org = await get_organization(session, ctx)
        if rule is None or org is None:
            raise SkipNotificationError("alert rule was deleted")
        if not rule.enabled:
            raise SkipNotificationError("alert rule is disabled")

        if row.channel == NotificationChannel.SLACK:
            if not limits_for(org.plan.value).slack_alerts:
                raise SkipNotificationError(
                    f"Slack alerts are not available on the {org.plan.value} plan"
                )
            if not rule.slack_webhook_ciphertext:
                raise SkipNotificationError("the rule no longer has a Slack webhook")
            try:
                url = self._box.decrypt(
                    rule.slack_webhook_ciphertext,
                    context=slack_webhook_context(row.tenant_id, rule.id),
                )
                url = validate_slack_webhook_url(url, allow_any_host=self._allow_any_slack_host)
            except (SecretBoxError, ValueError) as exc:
                raise DeliveryError(f"stored webhook unusable: {exc}", permanent=True) from exc
            await self._slack.send(
                url, render.slack_message(row.kind, row.payload, self._web_base_url)
            )
            return

        if row.channel == NotificationChannel.EMAIL:
            recipients = list(rule.email_recipients)
            if not recipients:
                raise SkipNotificationError("the rule no longer has e-mail recipients")
            subject, text = render.email_message(row.kind, row.payload, self._web_base_url)
            await self._email.send(recipients, subject, text)
            return
        raise DeliveryError(f"unknown channel {row.channel!r}", permanent=True)
