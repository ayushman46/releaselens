"""Audit-log helpers for request handlers, and the action catalogue.

Entries are written **in the same transaction** as the change they describe. IP and
user agent come from the request (IP via the trusted-proxy resolution in
:mod:`rl_api.clientip`). Secrets never go into ``metadata``: key entries carry the key's
public prefix, alert entries say *whether* a Slack webhook is set, never its URL.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from rl_api.clientip import client_ip, user_agent
from rl_api.principal import OrgAccess
from rl_core.tenancy import TenantContext
from rl_db.admin.audit import record_security_event
from rl_db.repositories.audit import AuditActor, AuditRequestInfo, record_audit

# The action catalogue (docs/security/AUTH.md §8).
LOGIN_SUCCEEDED: Final = "auth.login_succeeded"
LOGIN_FAILED: Final = "auth.login_failed"
LOGOUT: Final = "auth.logout"
REFRESH_REUSE: Final = "auth.refresh_token_reused"
PASSWORD_RESET: Final = "auth.password_reset"  # noqa: S105
EMAIL_VERIFIED: Final = "auth.email_verified"
SIGNED_UP: Final = "auth.signed_up"
GITHUB_LINKED: Final = "auth.github_linked"
ORG_CREATED: Final = "org.created"
ORG_DELETED: Final = "org.deleted"
SETTINGS_CHANGED: Final = "org.settings_changed"
MEMBER_INVITED: Final = "member.invited"
INVITE_REVOKED: Final = "member.invite_revoked"
MEMBER_JOINED: Final = "member.joined"
MEMBER_ROLE_CHANGED: Final = "member.role_changed"
MEMBER_REMOVED: Final = "member.removed"
KEY_CREATED: Final = "api_key.created"
KEY_REVOKED: Final = "api_key.revoked"
KEY_ROTATED: Final = "api_key.rotated"
PROJECT_CREATED: Final = "project.created"
PROJECT_UPDATED: Final = "project.updated"
PROJECT_DELETED: Final = "project.deleted"
ALERT_RULE_CREATED: Final = "alert_rule.created"
ALERT_RULE_UPDATED: Final = "alert_rule.updated"
ALERT_RULE_DELETED: Final = "alert_rule.deleted"
BILLING_CHECKOUT_STARTED: Final = "billing.checkout_started"
BILLING_SUBSCRIPTION_CHANGED: Final = "billing.subscription_changed"
BILLING_PAYMENT_FAILED: Final = "billing.payment_failed"
MODEL_PROMOTED: Final = "model.promoted"


def request_info(request: Request) -> AuditRequestInfo:
    return AuditRequestInfo(ip=client_ip(request), user_agent=user_agent(request))


def email_fingerprint(email: str) -> str:
    """Correlates failed sign-ins for one address without storing the address."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


async def audit(
    session: AsyncSession,
    access: OrgAccess,
    request: Request,
    action: str,
    *,
    target_type: str | None = None,
    target_id: object | None = None,
    details: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    return await record_audit(
        session,
        access.ctx,
        action=action,
        actor=access.actor(),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request=request_info(request),
        details=details,
    )


async def security_event(
    session: AsyncSession,
    request: Request,
    action: str,
    *,
    actor: AuditActor,
    details: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    return await record_security_event(
        session, action=action, actor=actor, request=request_info(request), details=details
    )


async def model_promoted(
    session: AsyncSession,
    *,
    model_name: str,
    version: str,
    alias: str,
    actor: AuditActor,
    tenant: TenantContext | None = None,
    details: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """Hook for the model registry (M4): record that ``model_name@version`` became
    ``alias`` (e.g. ``champion``). Tenant-less for the shared global model; pass ``tenant``
    for a tenant-specific model."""
    payload = {"model": model_name, "version": version, "alias": alias, **dict(details or {})}
    if tenant is None:
        return await record_security_event(
            session,
            action=MODEL_PROMOTED,
            actor=actor,
            target_type="model",
            target_id=f"{model_name}@{version}",
            details=payload,
        )
    return await record_audit(
        session,
        tenant,
        action=MODEL_PROMOTED,
        actor=actor,
        target_type="model",
        target_id=f"{model_name}@{version}",
        details=payload,
    )
