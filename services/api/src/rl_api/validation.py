"""Shared request-field types and error helpers for the M10 routers."""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator, StringConstraints

from rl_api.errors import ApiError
from rl_api.settings import Settings
from rl_core.plans import upgrade_target

# Deliberately simple: one "@", a dot in the domain, no whitespace or control characters.
# Deliverability is proven by the verification e-mail, not by a regex.
_EMAIL_RE = re.compile(
    r"^[^@\s\x00-\x1f\x7f]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253})\.[A-Za-z]{2,63}$"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _email(value: str) -> str:
    value = value.strip()
    if not _EMAIL_RE.fullmatch(value) or ".." in value.split("@", 1)[1]:
        raise ValueError("not a valid e-mail address")
    return value.lower()


def _no_control(value: str) -> str:
    if _CONTROL_RE.search(value):
        raise ValueError("must not contain control characters")
    return value


EmailStr = Annotated[str, StringConstraints(max_length=320), AfterValidator(_email)]
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(_no_control),
]
Slug = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
]
ProjectSlug = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
]


def not_found(what: str = "resource") -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def plan_limit_error(
    settings: Settings,
    *,
    limit: str,
    plan: str,
    maximum: int | None = None,
    current: int | None = None,
) -> ApiError:
    """``402 plan_limit_reached`` with the numbers and where to upgrade."""
    target = upgrade_target(plan)
    details: dict[str, Any] = {"limit": limit, "plan": plan}
    if maximum is not None:
        details["max"] = maximum
    if current is not None:
        details["current"] = current
    if target is not None:
        details["upgrade"] = {
            "plan": target,
            "url": settings.web_url("/app/settings/billing"),
        }
    noun = {"slack_alerts": "Slack alerts"}.get(limit, limit)
    message = (
        f"the {plan} plan allows at most {maximum} {noun}"
        if maximum is not None
        else f"{noun} are not available on the {plan} plan"
    )
    return ApiError(402, "plan_limit_reached", message, details=details)
