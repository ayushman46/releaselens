"""Rate-limit dependencies backed by Postgres token buckets (:mod:`rl_db.admin.ratelimit`).

* :func:`limit_ip` (auth endpoints): one bucket per client IP and policy. The IP comes from
  :func:`rl_api.clientip.client_ip`, so ``X-Forwarded-For`` only counts behind a trusted
  proxy.
* :func:`limit_api_key` (ingest endpoints): one bucket per API key, wrapping the endpoint's
  own scope dependency so the key is authenticated once.
* :func:`enforce` for limits keyed inside a handler (login per account and IP).

A refused request gets ``429 rate_limited`` with ``Retry-After``. Each decision is
committed in its own short transaction, so it counts even if the guarded request fails.
"""

# No "from __future__ import annotations": limit_api_key builds an Annotated type from a
# closure variable, which FastAPI must be able to evaluate at runtime.

import hashlib
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.clientip import client_ip
from rl_api.errors import ApiError
from rl_api.settings import RatePolicy, Settings
from rl_core.tenancy import TenantContext
from rl_db.admin.ratelimit import take_token

PolicyName = Literal["auth", "login_account", "ingest", "risk", "telemetry"]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def enforce(request: Request, policy: RatePolicy, subject: str) -> None:
    """Take a token for ``subject`` under ``policy`` or raise 429."""
    if not _settings(request).rate_limit_enabled:
        return
    # Hash the subject: bucket keys are bounded and never store e-mails or keys in clear.
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:40]
    sessions: async_sessionmaker[AsyncSession] = request.app.state.db_sessions
    async with sessions.begin() as session:
        decision = await take_token(
            session,
            f"{policy.name}:{digest}",
            capacity=policy.burst,
            refill_per_s=policy.refill_per_s,
        )
    if not decision.allowed:
        raise ApiError(
            429,
            "rate_limited",
            "too many requests; retry later",
            details={"retry_after_s": decision.retry_after_s},
            headers={"Retry-After": str(decision.retry_after_s)},
        )


def limit_ip(policy_name: PolicyName = "auth") -> Callable[[Request], Awaitable[None]]:
    async def dependency(request: Request) -> None:
        ip = client_ip(request) or "unknown"
        await enforce(request, _settings(request).rate_policy(policy_name), f"ip:{ip}")

    dependency.__name__ = f"rate_limit_ip_{policy_name}"
    return dependency


def limit_api_key(
    scope_dependency: Callable[..., Awaitable[TenantContext]],
    policy_name: PolicyName = "ingest",
) -> Callable[..., Awaitable[TenantContext]]:
    """Wrap an API-key scope dependency: authenticate, then limit per key."""

    async def dependency(
        request: Request, ctx: Annotated[TenantContext, Depends(scope_dependency)]
    ) -> TenantContext:
        await enforce(request, _settings(request).rate_policy(policy_name), f"key:{ctx.actor_id}")
        return ctx

    dependency.__name__ = f"rate_limit_key_{policy_name}"
    return dependency
