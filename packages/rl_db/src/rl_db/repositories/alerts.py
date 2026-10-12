"""Alert rules and notification enqueueing (tenant-scoped, ADR-0010)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.alerts import (
    AlertRule,
    NotificationChannel,
    NotificationKind,
    NotificationLog,
    NotificationStatus,
)

# Order of the severity / risk bands that can trigger an alert.
_BAND_RANK = {"HEALTHY": 0, "LOW": 0, "WARNING": 1, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def band_meets(band: str | None, minimum: str) -> bool:
    """True if ``band`` is at least ``minimum`` (``HIGH`` or ``CRITICAL``)."""
    if band is None:
        return False
    return _BAND_RANK.get(band.upper(), -1) >= _BAND_RANK[minimum]


async def create_alert_rule(
    session: AsyncSession, ctx: TenantContext, *, project_id: uuid.UUID, values: dict[str, Any]
) -> AlertRule:
    rule_id = values.pop("id", None) or uuid7()
    row = AlertRule(id=rule_id, tenant_id=ctx.tenant_id, project_id=project_id, **values)
    session.add(row)
    await session.flush()
    return row


async def list_alert_rules(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID
) -> list[AlertRule]:
    stmt = (
        select(AlertRule)
        .where(AlertRule.tenant_id == ctx.tenant_id, AlertRule.project_id == project_id)
        .order_by(AlertRule.created_at, AlertRule.id)
    )
    return list((await session.execute(stmt)).scalars())


async def get_alert_rule(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, rule_id: uuid.UUID
) -> AlertRule | None:
    stmt = select(AlertRule).where(
        AlertRule.tenant_id == ctx.tenant_id,
        AlertRule.project_id == project_id,
        AlertRule.id == rule_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_alert_rule_by_id(
    session: AsyncSession, ctx: TenantContext, rule_id: uuid.UUID
) -> AlertRule | None:
    """A rule by id alone (the dispatcher knows the rule, not its project)."""
    stmt = select(AlertRule).where(AlertRule.tenant_id == ctx.tenant_id, AlertRule.id == rule_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_alert_rule(
    session: AsyncSession,
    ctx: TenantContext,
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    values: dict[str, Any],
) -> AlertRule | None:
    if not values:
        return await get_alert_rule(session, ctx, project_id, rule_id)
    stmt = (
        update(AlertRule)
        .where(
            AlertRule.tenant_id == ctx.tenant_id,
            AlertRule.project_id == project_id,
            AlertRule.id == rule_id,
        )
        .values(**values, updated_at=func.now())
        .returning(AlertRule)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_alert_rule(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, rule_id: uuid.UUID
) -> bool:
    result = await session.execute(
        delete(AlertRule).where(
            AlertRule.tenant_id == ctx.tenant_id,
            AlertRule.project_id == project_id,
            AlertRule.id == rule_id,
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def count_slack_rules(session: AsyncSession, ctx: TenantContext) -> int:
    stmt = select(func.count()).where(
        AlertRule.tenant_id == ctx.tenant_id, AlertRule.slack_webhook_ciphertext.is_not(None)
    )
    return int((await session.execute(stmt)).scalar_one())


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    matched_rules: int
    created: int  # new notification_log rows (0 on a redelivery)


async def enqueue_notifications(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    project_id: uuid.UUID,
    kind: str,
    subject_id: uuid.UUID,
    band: str | None,
    payload: Mapping[str, Any],
) -> EnqueueResult:
    """Queue one notification per matching rule and channel, idempotently.

    A rule matches when it is enabled, belongs to ``project_id``, has the trigger ``kind``
    switched on and ``band`` reaches its minimum. ``INSERT … ON CONFLICT DO NOTHING`` on
    the unique key (tenant, rule, subject, kind, channel) makes a redelivered event, or a
    later analysis of the same deployment, a no-op. Whether Slack is allowed on the
    tenant's plan is decided at send time, so a plan change applies to queued rows too.
    """
    if kind not in (NotificationKind.REGRESSION, NotificationKind.RISK):
        raise ValueError(f"unknown notification kind {kind!r}")
    rules = await list_alert_rules(session, ctx, project_id)
    matched: list[AlertRule] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if kind == NotificationKind.REGRESSION and rule.on_regression:
            if band_meets(band, rule.regression_min_band):
                matched.append(rule)
        elif (
            kind == NotificationKind.RISK and rule.on_risk and band_meets(band, rule.risk_min_band)
        ):
            matched.append(rule)
    rows: list[dict[str, Any]] = []
    for rule in matched:
        channels: Sequence[str] = [
            *([NotificationChannel.SLACK] if rule.slack_webhook_ciphertext else []),
            *([NotificationChannel.EMAIL] if rule.email_recipients else []),
        ]
        rows.extend(
            {
                "id": uuid7(),
                "tenant_id": ctx.tenant_id,
                "alert_rule_id": rule.id,
                "kind": kind,
                "subject_id": subject_id,
                "channel": channel,
                "payload": dict(payload),
            }
            for channel in channels
        )
    if not rows:
        return EnqueueResult(matched_rules=len(matched), created=0)
    stmt = (
        pg_insert(NotificationLog)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_notification_log_idempotency")
        .returning(NotificationLog.id)
    )
    created = len((await session.execute(stmt)).all())
    return EnqueueResult(matched_rules=len(matched), created=created)


async def list_notifications(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int,
    status: str | None = None,
    before: datetime | None = None,
) -> list[NotificationLog]:
    stmt = select(NotificationLog).where(NotificationLog.tenant_id == ctx.tenant_id)
    if status is not None:
        stmt = stmt.where(NotificationLog.status == status)
    if before is not None:
        stmt = stmt.where(NotificationLog.created_at < before)
    stmt = stmt.order_by(NotificationLog.created_at.desc(), NotificationLog.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


__all__ = [
    "EnqueueResult",
    "NotificationStatus",
    "band_meets",
    "count_slack_rules",
    "create_alert_rule",
    "delete_alert_rule",
    "enqueue_notifications",
    "get_alert_rule",
    "get_alert_rule_by_id",
    "list_alert_rules",
    "list_notifications",
    "update_alert_rule",
]
