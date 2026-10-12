"""API-key authentication and scope checks (FastAPI dependencies).

A key is accepted from ``Authorization: Bearer <key>`` or ``x-releaselens-key: <key>``. The
second header is what the OTel Collector's ``headers`` setting sends (ARCHITECTURE.md §1).

Every failure answers the same ``401 unauthorized``: missing header, malformed key, unknown
prefix, wrong secret, revoked or expired key. Distinguishing them would tell an attacker
which part of a guessed or leaked key was right. The specific reason is logged
server-side.

The tenant comes **only** from the key's row. Nothing in the request (body, query, headers)
can choose or override it (ADR-0010).
"""

from __future__ import annotations

import hmac
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.apikeys import KNOWN_SCOPES, hash_api_key, parse_prefix
from rl_api.errors import ApiError
from rl_core.obs import bind_log_context
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.identity import find_api_key_by_prefix, touch_api_key

log = logging.getLogger("rl_api.auth")

API_KEY_HEADER = "x-releaselens-key"
# Compared against when the prefix is unknown, so that path costs the same hash + compare
# as a wrong secret.
_DUMMY_HASH = hash_api_key("rlk_000000000000_" + "0" * 43)


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "missing or invalid API key",
        headers={"WWW-Authenticate": 'Bearer realm="releaselens"'},
    )


def _presented_key(request: Request) -> str | None:
    bearer: str | None = None
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            bearer = value.strip()
    alternate = (request.headers.get(API_KEY_HEADER) or "").strip() or None
    # Bytes, not str: compare_digest raises TypeError on non-ASCII strings, and header values
    # are attacker-controlled (Starlette decodes them as latin-1).
    if (
        bearer
        and alternate
        and not hmac.compare_digest(bearer.encode("utf-8"), alternate.encode("utf-8"))
    ):
        # Two different keys in one request: refuse rather than guess which one counts.
        raise _unauthorized()
    return bearer or alternate


async def _record_usage(
    sessions: async_sessionmaker[AsyncSession],
    key_id: uuid.UUID,
    now: datetime,
    min_interval: timedelta,
) -> None:
    """Runs after the response is sent; usage tracking must never fail a request."""
    try:
        async with sessions.begin() as session:
            await touch_api_key(session, key_id, now=now, min_interval=min_interval)
    except Exception:
        log.warning("could not record API key usage", exc_info=True)


async def authenticate(request: Request, background: BackgroundTasks) -> TenantContext:
    presented = _presented_key(request)
    if presented is None:
        raise _unauthorized()
    prefix = parse_prefix(presented)
    if prefix is None:
        log.info("api key rejected", extra={"reason": "malformed"})
        raise _unauthorized()

    sessions: async_sessionmaker[AsyncSession] = request.app.state.db_sessions
    # A short session of its own, so the lookup's implicit transaction never leaks into
    # the route's session.
    async with sessions() as session:
        credential = await find_api_key_by_prefix(session, prefix)

    if credential is None:
        hmac.compare_digest(hash_api_key(presented), _DUMMY_HASH)
        log.info("api key rejected", extra={"reason": "unknown_prefix", "key_prefix": prefix})
        raise _unauthorized()
    if not hmac.compare_digest(hash_api_key(presented), credential.secret_hash):
        log.info("api key rejected", extra={"reason": "bad_secret", "key_prefix": prefix})
        raise _unauthorized()
    now = datetime.now(UTC)
    # Any recorded revocation counts, whatever its timestamp. Comparing it with this
    # process's clock would let a revoked key keep working for as long as the revoking
    # host's clock runs ahead of ours. Planned end-of-life is what expires_at is for.
    if credential.revoked_at is not None:
        log.info("api key rejected", extra={"reason": "revoked", "key_prefix": prefix})
        raise _unauthorized()
    if credential.expires_at is not None and credential.expires_at <= now:
        log.info("api key rejected", extra={"reason": "expired", "key_prefix": prefix})
        raise _unauthorized()

    ctx = TenantContext(
        tenant_id=credential.tenant_id,
        principal=Principal.API_KEY,
        actor_id=credential.id,
        scopes=credential.scopes,
    )
    # Tag the rest of this request's logs (including the access log) with the tenant.
    bind_log_context(tenant_id=str(ctx.tenant_id), api_key_id=str(credential.id))
    request.state.tenant = ctx

    interval = timedelta(seconds=request.app.state.settings.api_key_touch_interval_s)
    if credential.last_used_at is None or now - credential.last_used_at >= interval:
        background.add_task(_record_usage, sessions, credential.id, now, interval)
    return ctx


def require_scope(scope: str) -> Callable[..., Awaitable[TenantContext]]:
    """Dependency factory: authenticate, then demand ``scope`` (403 if the key lacks it)."""
    if scope not in KNOWN_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    async def dependency(ctx: TenantContext = Depends(authenticate)) -> TenantContext:  # noqa: B008
        if not ctx.has_scope(scope):
            raise ApiError(403, "insufficient_scope", f"this API key lacks the {scope!r} scope")
        return ctx

    dependency.__name__ = f"require_scope_{scope.replace(':', '_')}"
    return dependency
