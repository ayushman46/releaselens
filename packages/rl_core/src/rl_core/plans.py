"""Plans and their limits (spec §13; docs/billing/BILLING.md).

The single source of truth for what each plan allows. The API enforces the limits when a
resource is created, the worker when it sends a notification or purges old data, and the
web app shows them on the pricing page (``GET /api/v1/plans``). Prices are list prices for
display; what a customer is actually charged is whatever the Stripe price says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

PlanId = Literal["free", "team", "enterprise"]
PLAN_IDS: Final[tuple[PlanId, ...]] = ("free", "team", "enterprise")


@dataclass(frozen=True, slots=True)
class PlanLimits:
    plan: PlanId
    name: str
    #: ``None`` means "no fixed limit" (Enterprise terms are agreed per contract).
    max_projects: int | None
    max_services: int | None
    #: Telemetry and deployment data older than this many days is purged.
    retention_days: int
    slack_alerts: bool
    github_checks: bool
    #: List price in whole US dollars per month; ``None`` for "contact us".
    price_usd_month: int | None
    self_serve: bool


FREE: Final = PlanLimits(
    plan="free",
    name="Free",
    max_projects=1,
    max_services=3,
    retention_days=7,
    slack_alerts=False,
    github_checks=False,
    price_usd_month=0,
    self_serve=True,
)
TEAM: Final = PlanLimits(
    plan="team",
    name="Team",
    max_projects=10,
    max_services=50,
    retention_days=30,
    slack_alerts=True,
    github_checks=True,
    price_usd_month=99,
    self_serve=True,
)
# Enterprise limits are contractual. The retention used by the purge job until a contract
# sets another value is 90 days; there is no self-serve checkout for this plan.
ENTERPRISE: Final = PlanLimits(
    plan="enterprise",
    name="Enterprise",
    max_projects=None,
    max_services=None,
    retention_days=90,
    slack_alerts=True,
    github_checks=True,
    price_usd_month=None,
    self_serve=False,
)

PLANS: Final[dict[str, PlanLimits]] = {p.plan: p for p in (FREE, TEAM, ENTERPRISE)}

LimitName = Literal["projects", "services", "slack_alerts"]


def limits_for(plan: str) -> PlanLimits:
    """Limits of ``plan``. An unknown plan id gets the Free limits (fail closed)."""
    return PLANS.get(plan, FREE)


def upgrade_target(plan: str) -> PlanId | None:
    """The next plan up, for the "upgrade" hint attached to a ``plan_limit_reached`` error."""
    if plan == "free":
        return "team"
    if plan == "team":
        return "enterprise"
    return None


def allows_count(limit: int | None, current: int) -> bool:
    """True if one more item fits: ``current`` items exist and the limit is ``limit``."""
    return limit is None or current < limit
