"""Turn events into queued notifications (``notification_log`` rows).

* :func:`enqueue_for_analysis`: one ``analysis.results`` envelope (the contract in
  ``rl_core.events.analysis``; docs/events/ANALYSIS_RESULTS.md). It notifies when
  ``payload.severity_band`` is HIGH or CRITICAL and reaches the rule's minimum.
* :func:`notify_risk`: the pre-deploy hook the risk-assessment path (M5) calls when an
  assessment's band is HIGH or CRITICAL. It lives in :mod:`rl_db.repositories.alerts` (the API
  calls it in the assessment's transaction) and is re-exported here.

The deployed ``service`` is mapped to its project through the ``services`` table (names
are unique per tenant); an unregistered service has no project and therefore no rules.

Idempotency: one row per (rule, subject, kind, channel). ``subject`` is the deployment id
for regressions, so ``early_5m``, ``early_10m`` and ``final`` analyses of one deployment
alert **once** per rule and channel: the first HIGH/CRITICAL result wins, later ones (and
redeliveries) are no-ops. A later escalation from HIGH to CRITICAL does not re-alert
(docs/security/AUTH.md §9 lists this as an open issue).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.events.analysis import AnalysisEnvelope
from rl_core.tenancy import Principal, TenantContext
from rl_db.models import NotificationKind
from rl_db.repositories.alerts import (
    EnqueueResult,
    band_meets,
    enqueue_notifications,
    notify_risk,
)
from rl_db.repositories.projects import find_service_by_name

ALERT_BANDS = ("HIGH", "CRITICAL")
NOTHING = EnqueueResult(matched_rules=0, created=0)


async def enqueue_for_analysis(session: AsyncSession, envelope: AnalysisEnvelope) -> EnqueueResult:
    summary = envelope.payload
    if not band_meets(summary.severity_band, "HIGH"):
        return NOTHING
    ctx = TenantContext(tenant_id=envelope.tenant_id, principal=Principal.SYSTEM)
    service = await find_service_by_name(session, ctx, summary.service)
    if service is None:
        return NOTHING
    payload: dict[str, Any] = summary.model_dump(mode="json")
    payload["event_id"] = str(envelope.event_id)
    return await enqueue_notifications(
        session,
        ctx,
        project_id=service.project_id,
        kind=NotificationKind.REGRESSION,
        subject_id=summary.deployment_id,
        band=summary.severity_band,
        payload=payload,
    )


__all__ = ["ALERT_BANDS", "NOTHING", "enqueue_for_analysis", "notify_risk"]
