"""Plans, billing status, Stripe Checkout / Customer Portal and the Stripe webhook.

Without ``RL_STRIPE_SECRET_KEY`` (and ``RL_STRIPE_PRICE_TEAM`` for checkout) the Stripe
endpoints answer ``503 billing_not_configured``; nothing pretends a payment succeeded.
Only Stripe **test-mode** keys are accepted (settings validation).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import audit as audit_log
from rl_api.billing import (
    BillingError,
    BillingGateway,
    InvalidSignatureError,
    StripeEventHandler,
    verify_webhook,
)
from rl_api.deps import get_app_settings, get_billing_gateway, get_sessions
from rl_api.errors import ApiError
from rl_api.principal import OrgAccess, require_org
from rl_api.routers.orgs import PlanLimitsOut, limits_out
from rl_api.settings import Settings
from rl_api.validation import not_found
from rl_core.plans import PLANS, limits_for
from rl_db.models import Role
from rl_db.repositories import billing as billing_repo
from rl_db.repositories import catalog
from rl_db.repositories import orgs as orgs_repo
from rl_db.repositories.billing import month_start

log = logging.getLogger("rl_api.billing")

router = APIRouter(prefix="/api/v1", tags=["billing"])

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]
GatewayDep = Annotated[BillingGateway | None, Depends(get_billing_gateway)]
ViewerSession = Annotated[OrgAccess, Depends(require_org(Role.VIEWER))]
Owner = Annotated[OrgAccess, Depends(require_org(Role.OWNER))]


class SubscriptionOut(BaseModel):
    status: str
    plan: str
    current_period_end: datetime | None
    cancel_at_period_end: bool
    payment_failed_at: datetime | None


class UsageOut(BaseModel):
    projects: int
    services: int
    deployments_this_month: int


class BillingOut(BaseModel):
    plan: str
    limits: PlanLimitsOut
    usage: UsageOut
    subscription: SubscriptionOut | None
    stripe_configured: bool


class CheckoutIn(BaseModel):
    plan: Literal["team"] = "team"


class RedirectUrl(BaseModel):
    url: str


class WebhookAck(BaseModel):
    status: str


def _not_configured() -> ApiError:
    return ApiError(503, "billing_not_configured", "billing is not configured on this server")


@router.get("/plans", response_model=list[PlanLimitsOut], summary="Plans and their limits (public)")
async def list_plans() -> list[PlanLimitsOut]:
    return [limits_out(p) for p in PLANS.values()]


@router.get(
    "/orgs/{org_id}/billing", response_model=BillingOut, summary="Plan, usage and subscription"
)
async def get_billing(
    access: ViewerSession, settings: SettingsDep, sessions: SessionsDep
) -> BillingOut:
    async with sessions() as db:
        org = await orgs_repo.get_organization(db, access.ctx)
        if org is None:
            raise not_found("organization")
        sub = await billing_repo.get_subscription(db, access.ctx)
        usage = await billing_repo.get_usage(
            db, access.ctx, period_start=month_start(datetime.now(UTC).date())
        )
        projects = await catalog.count_projects(db, access.ctx)
        services = await catalog.count_services(db, access.ctx)
    return BillingOut(
        plan=org.plan.value,
        limits=limits_out(limits_for(org.plan.value)),
        usage=UsageOut(
            projects=projects,
            services=services,
            deployments_this_month=usage.get("deployments", 0),
        ),
        subscription=(
            SubscriptionOut(
                status=sub.status,
                plan=sub.plan.value,
                current_period_end=sub.current_period_end,
                cancel_at_period_end=sub.cancel_at_period_end,
                payment_failed_at=sub.payment_failed_at,
            )
            if sub is not None
            else None
        ),
        stripe_configured=settings.stripe_configured,
    )


@router.post(
    "/orgs/{org_id}/billing/checkout",
    response_model=RedirectUrl,
    summary="Start a Stripe Checkout session for the Team plan (owner)",
    responses={409: {"description": "Already subscribed"}, 503: {"description": "Not configured"}},
)
async def start_checkout(
    body: CheckoutIn,
    request: Request,
    access: Owner,
    settings: SettingsDep,
    sessions: SessionsDep,
    gateway: GatewayDep,
) -> RedirectUrl:
    if gateway is None or not settings.stripe_price_team:
        raise _not_configured()
    async with sessions() as db:
        sub = await billing_repo.get_subscription(db, access.ctx)
    if sub is not None and sub.status in ("active", "trialing", "past_due"):
        raise ApiError(409, "already_subscribed", "manage the subscription in the billing portal")
    try:
        url = await gateway.create_checkout_session(
            price_id=settings.stripe_price_team,
            tenant_id=access.org_id,
            customer_id=sub.stripe_customer_id if sub else None,
            customer_email=access.user.email if access.user else None,
            success_url=settings.web_url("/app/settings/billing?checkout=success"),
            cancel_url=settings.web_url("/app/settings/billing?checkout=cancelled"),
        )
    except BillingError as exc:
        log.warning("checkout session failed", extra={"error": str(exc)})
        raise ApiError(
            502, "billing_provider_error", "the payment provider is unavailable"
        ) from exc
    async with sessions.begin() as db:
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.BILLING_CHECKOUT_STARTED,
            target_type="billing",
            details={"plan": body.plan},
        )
    return RedirectUrl(url=url)


@router.post(
    "/orgs/{org_id}/billing/portal",
    response_model=RedirectUrl,
    summary="Open the Stripe Customer Portal (owner)",
    responses={
        409: {"description": "No billing account yet"},
        503: {"description": "Not configured"},
    },
)
async def open_portal(
    access: Owner, settings: SettingsDep, sessions: SessionsDep, gateway: GatewayDep
) -> RedirectUrl:
    if gateway is None:
        raise _not_configured()
    async with sessions() as db:
        sub = await billing_repo.get_subscription(db, access.ctx)
    if sub is None:
        raise ApiError(409, "no_billing_account", "subscribe first to manage billing")
    try:
        url = await gateway.create_portal_session(
            customer_id=sub.stripe_customer_id, return_url=settings.web_url("/app/settings/billing")
        )
    except BillingError as exc:
        log.warning("portal session failed", extra={"error": str(exc)})
        raise ApiError(
            502, "billing_provider_error", "the payment provider is unavailable"
        ) from exc
    return RedirectUrl(url=url)


@router.post(
    "/billing/stripe/webhook",
    response_model=WebhookAck,
    summary="Stripe webhook (signature-verified, idempotent by event id)",
    responses={400: {"description": "Bad signature"}, 503: {"description": "Not configured"}},
)
async def stripe_webhook(
    request: Request,
    settings: SettingsDep,
    sessions: SessionsDep,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> WebhookAck:
    if settings.stripe_webhook_secret is None:
        raise _not_configured()
    payload = await request.body()
    try:
        event = verify_webhook(
            payload, stripe_signature, settings.stripe_webhook_secret.get_secret_value()
        )
    except InvalidSignatureError as exc:
        log.warning("stripe webhook rejected", extra={"reason": str(exc)[:200]})
        raise ApiError(400, "invalid_signature", "webhook signature verification failed") from exc
    handler = StripeEventHandler(team_price_id=settings.stripe_price_team)
    # One transaction: the event id is recorded only if its effects commit. On failure
    # Stripe retries, and the retry is processed from scratch.
    async with sessions.begin() as db:
        outcome = await handler.apply(db, event)
    log.info(
        "stripe event",
        extra={
            "event_id": event.get("id"),
            "type": event.get("type"),
            "outcome": outcome.status,
            "tenant_id": str(outcome.tenant_id) if outcome.tenant_id else None,
        },
    )
    return WebhookAck(status=outcome.status)
