"""Turn events into queued notifications (``notification_log`` rows).

* :func:`enqueue_for_analysis`: one ``analysis.results`` envelope (the contract in
  ``rl_core.events.analysis``; docs/events/ANALYSIS_RESULTS.md). It notifies when
  ``payload.severity_band`` is HIGH or CRITICAL and reaches the rule's minimum.
* :func:`notify_risk`: the pre-deploy hook for the risk-assessment path (M5) to call when an
  assessment's band is HIGH or CRITICAL.

The deployed ``service`` is mapped to its project through the ``services`` table (names
are unique per tenant); an unregistered service has no project and therefore no rules.

Idempotency: one row per (rule, subject, kind, channel). ``subject`` is the deployment id
for regressions, so ``early_5m``, ``early_10m`` and ``final`` analyses of one deployment
alert **once** per rule and channel: the first HIGH/CRITICAL result wins, later ones (and
redeliveries) are no-ops. A later escalation from HIGH to CRITICAL does not re-alert
(docs/security/AUTH.md §9 lists this as an open issue).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.events.analysis import AnalysisEnvelope
from rl_core.tenancy import Principal, TenantContext
from rl_db.models import NotificationKind
from rl_db.repositories.alerts import EnqueueResult, band_meets, enqueue_notifications
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


async def notify_risk(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    risk_assessment_id: uuid.UUID,
    band: str,
    service: str | None = None,
    project_id: uuid.UUID | None = None,
    risk_score: int | float | None = None,
    repository: str | None = None,
    commit_sha: str | None = None,
) -> EnqueueResult:
    """Queue pre-deploy risk alerts for a HIGH/CRITICAL assessment, in the caller's
    transaction (so they exist if and only if the assessment was stored). Give the
    ``project_id`` or the ``service`` it belongs to."""
    if not band_meets(band, "HIGH"):
        return NOTHING
    if project_id is None and service is not None:
        found = await find_service_by_name(session, ctx, service)
        project_id = found.project_id if found else None
    if project_id is None:
        return NOTHING
    payload = {
        "risk_assessment_id": str(risk_assessment_id),
        "band": band.upper(),
        "risk_score": risk_score,
        "service": service,
        "repository": repository,
        "commit_sha": commit_sha,
    }
    return await enqueue_notifications(
        session,
        ctx,
        project_id=project_id,
        kind=NotificationKind.RISK,
        subject_id=risk_assessment_id,
        band=band,
        payload=payload,
    )
