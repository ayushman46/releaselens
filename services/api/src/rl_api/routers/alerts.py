"""Alert rules per project, and the notification log.

The Slack webhook URL is write-only: it is validated (``hooks.slack.com`` only outside
dev/test), encrypted with AES-256-GCM bound to the rule's row (:mod:`rl_db.secretbox`) and
never returned; responses show ``{"configured": true, "hint": "https://hooks.slack.com/…abcd"}``.
Slack alerts are a Team feature: setting a webhook on the Free plan answers
``402 plan_limit_reached``. Rules are managed by admins; any member can read them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import audit as audit_log
from rl_api.deps import get_app_settings, get_secret_box, get_sessions
from rl_api.errors import ApiError
from rl_api.principal import OrgAccess, require_org
from rl_api.settings import Settings
from rl_api.validation import EmailStr, Name, not_found, plan_limit_error
from rl_core.alerting import validate_slack_webhook_url, webhook_hint
from rl_core.ids import uuid7
from rl_core.plans import limits_for
from rl_db.models import AlertRule, NotificationLog, Role
from rl_db.models.alerts import MAX_EMAIL_RECIPIENTS
from rl_db.repositories import alerts as alerts_repo
from rl_db.repositories import orgs as orgs_repo
from rl_db.repositories import projects as catalog
from rl_db.secretbox import SecretBox, slack_webhook_context

router = APIRouter(prefix="/api/v1/orgs/{org_id}", tags=["alerts"])

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]
BoxDep = Annotated[SecretBox, Depends(get_secret_box)]
Reader = Annotated[OrgAccess, Depends(require_org(Role.VIEWER, api_key_scope="read"))]
ViewerSession = Annotated[OrgAccess, Depends(require_org(Role.VIEWER))]
Admin = Annotated[OrgAccess, Depends(require_org(Role.ADMIN))]

Band = Literal["HIGH", "CRITICAL"]


class _RuleFields(BaseModel):
    name: Name | None = None
    enabled: bool | None = None
    on_regression: bool | None = None
    regression_min_band: Band | None = None
    on_risk: bool | None = None
    risk_min_band: Band | None = None
    # Write-only. Absent: unchanged; null: removed; string: replaced.
    slack_webhook_url: str | None = Field(default=None, max_length=512)
    email_recipients: list[EmailStr] | None = Field(default=None, max_length=MAX_EMAIL_RECIPIENTS)

    @field_validator("email_recipients")
    @classmethod
    def _dedupe(cls, value: list[str] | None) -> list[str] | None:
        return sorted(set(value)) if value is not None else None


class AlertRuleCreate(_RuleFields):
    name: Name


class AlertRuleUpdate(_RuleFields):
    @model_validator(mode="after")
    def _not_empty(self) -> AlertRuleUpdate:
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        return self


class SlackOut(BaseModel):
    configured: bool
    hint: str | None


class AlertRuleOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    enabled: bool
    on_regression: bool
    regression_min_band: str
    on_risk: bool
    risk_min_band: str
    slack: SlackOut
    email_recipients: list[str]
    created_at: datetime
    updated_at: datetime


class NotificationOut(BaseModel):
    id: uuid.UUID
    alert_rule_id: uuid.UUID | None
    kind: str
    subject_id: uuid.UUID
    channel: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None


def rule_out(rule: AlertRule) -> AlertRuleOut:
    return AlertRuleOut(
        id=rule.id,
        project_id=rule.project_id,
        name=rule.name,
        enabled=rule.enabled,
        on_regression=rule.on_regression,
        regression_min_band=rule.regression_min_band,
        on_risk=rule.on_risk,
        risk_min_band=rule.risk_min_band,
        slack=SlackOut(
            configured=rule.slack_webhook_ciphertext is not None, hint=rule.slack_webhook_hint
        ),
        email_recipients=list(rule.email_recipients),
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _audit_details(values: dict[str, Any]) -> dict[str, Any]:
    """What changed, without the secret."""
    out = {
        k: v
        for k, v in values.items()
        if k not in ("slack_webhook_ciphertext", "slack_webhook_hint")
    }
    if "slack_webhook_ciphertext" in values:
        out["slack_webhook"] = "set" if values["slack_webhook_ciphertext"] else "removed"
    if "email_recipients" in out:
        out["email_recipients"] = len(out["email_recipients"])
    return out


async def _rule_values(
    db: AsyncSession,
    access: OrgAccess,
    body: _RuleFields,
    *,
    rule_id: uuid.UUID,
    settings: Settings,
    box: SecretBox,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in (
        "name",
        "enabled",
        "on_regression",
        "regression_min_band",
        "on_risk",
        "risk_min_band",
    ):
        if field in body.model_fields_set and getattr(body, field) is not None:
            values[field] = getattr(body, field)
    if "email_recipients" in body.model_fields_set:
        values["email_recipients"] = body.email_recipients or []
    if "slack_webhook_url" in body.model_fields_set:
        url = body.slack_webhook_url
        if url is None:
            values.update(slack_webhook_ciphertext=None, slack_webhook_hint=None)
        else:
            try:
                url = validate_slack_webhook_url(url, allow_any_host=settings.allow_any_slack_host)
            except ValueError as exc:
                raise ApiError(422, "invalid_webhook_url", str(exc)) from exc
            org = await orgs_repo.get_organization(db, access.ctx)
            plan = org.plan.value if org else "free"
            if not limits_for(plan).slack_alerts:
                raise plan_limit_error(settings, limit="slack_alerts", plan=plan)
            values.update(
                slack_webhook_ciphertext=box.encrypt(
                    url, context=slack_webhook_context(access.org_id, rule_id)
                ),
                slack_webhook_hint=webhook_hint(url),
            )
    return values


async def _project(db: AsyncSession, access: OrgAccess, project_id: uuid.UUID) -> None:
    if await catalog.get_project(db, access.ctx, project_id) is None:
        raise not_found("project")


@router.get(
    "/projects/{project_id}/alert-rules",
    response_model=list[AlertRuleOut],
    summary="Alert rules of a project",
)
async def list_rules(
    project_id: uuid.UUID, access: Reader, sessions: SessionsDep
) -> list[AlertRuleOut]:
    async with sessions() as db:
        await _project(db, access, project_id)
        return [rule_out(r) for r in await alerts_repo.list_alert_rules(db, access.ctx, project_id)]


@router.post(
    "/projects/{project_id}/alert-rules",
    status_code=201,
    response_model=AlertRuleOut,
    summary="Create an alert rule (admin)",
    responses={402: {"description": "Slack alerts need the Team plan"}, 422: {}},
)
async def create_rule(
    project_id: uuid.UUID,
    body: AlertRuleCreate,
    request: Request,
    access: Admin,
    settings: SettingsDep,
    sessions: SessionsDep,
    box: BoxDep,
) -> AlertRuleOut:
    rule_id = uuid7()
    async with sessions.begin() as db:
        await _project(db, access, project_id)
        values = await _rule_values(db, access, body, rule_id=rule_id, settings=settings, box=box)
        values["created_by"] = access.user.user_id if access.user else None
        rule = await alerts_repo.create_alert_rule(
            db, access.ctx, project_id=project_id, values={"id": rule_id, **values}
        )
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.ALERT_RULE_CREATED,
            target_type="alert_rule",
            target_id=rule.id,
            details=_audit_details({k: v for k, v in values.items() if k != "created_by"}),
        )
        fresh = await alerts_repo.get_alert_rule(db, access.ctx, project_id, rule.id)
    assert fresh is not None
    return rule_out(fresh)


@router.get(
    "/projects/{project_id}/alert-rules/{rule_id}",
    response_model=AlertRuleOut,
    summary="One alert rule",
)
async def get_rule(
    project_id: uuid.UUID, rule_id: uuid.UUID, access: Reader, sessions: SessionsDep
) -> AlertRuleOut:
    async with sessions() as db:
        rule = await alerts_repo.get_alert_rule(db, access.ctx, project_id, rule_id)
    if rule is None:
        raise not_found("alert rule")
    return rule_out(rule)


@router.patch(
    "/projects/{project_id}/alert-rules/{rule_id}",
    response_model=AlertRuleOut,
    summary="Update an alert rule (admin)",
    responses={402: {"description": "Slack alerts need the Team plan"}, 422: {}},
)
async def update_rule(
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: AlertRuleUpdate,
    request: Request,
    access: Admin,
    settings: SettingsDep,
    sessions: SessionsDep,
    box: BoxDep,
) -> AlertRuleOut:
    async with sessions.begin() as db:
        if await alerts_repo.get_alert_rule(db, access.ctx, project_id, rule_id) is None:
            raise not_found("alert rule")
        values = await _rule_values(db, access, body, rule_id=rule_id, settings=settings, box=box)
        rule = await alerts_repo.update_alert_rule(db, access.ctx, project_id, rule_id, values)
        if rule is None:
            raise not_found("alert rule")
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.ALERT_RULE_UPDATED,
            target_type="alert_rule",
            target_id=rule_id,
            details=_audit_details(values),
        )
    return rule_out(rule)


@router.delete(
    "/projects/{project_id}/alert-rules/{rule_id}",
    status_code=204,
    summary="Delete an alert rule (admin)",
)
async def delete_rule(
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    request: Request,
    access: Admin,
    sessions: SessionsDep,
) -> Response:
    async with sessions.begin() as db:
        if not await alerts_repo.delete_alert_rule(db, access.ctx, project_id, rule_id):
            raise not_found("alert rule")
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.ALERT_RULE_DELETED,
            target_type="alert_rule",
            target_id=rule_id,
        )
    return Response(status_code=204)


@router.get(
    "/notifications",
    response_model=list[NotificationOut],
    summary="Notification delivery log (any member)",
)
async def list_notifications(
    access: ViewerSession,
    sessions: SessionsDep,
    status: Annotated[
        Literal["pending", "sending", "sent", "failed", "dead", "skipped"] | None, Query()
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[NotificationOut]:
    async with sessions() as db:
        rows: list[NotificationLog] = await alerts_repo.list_notifications(
            db, access.ctx, limit=limit, status=status
        )
    return [
        NotificationOut(
            id=r.id,
            alert_rule_id=r.alert_rule_id,
            kind=r.kind,
            subject_id=r.subject_id,
            channel=r.channel,
            status=r.status,
            attempts=r.attempts,
            last_error=r.last_error,
            created_at=r.created_at,
            sent_at=r.sent_at,
        )
        for r in rows
    ]
