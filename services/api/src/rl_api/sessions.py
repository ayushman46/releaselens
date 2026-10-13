"""Browser sessions: opaque tokens in cookies (proposed ADR "session design";
docs/security/AUTH.md §3).

* ``rl_access``: opaque access token, **15 minutes**, ``HttpOnly; Secure; SameSite=Lax;
  Path=/``. Looked up (by hash) on every request, so logout and revocation take effect
  immediately.
* ``rl_refresh``: opaque refresh token, **14 days** (never past the session's 30-day
  absolute limit), ``HttpOnly; Secure; SameSite=Lax; Path=/api/v1/auth`` so it is only
  sent to the refresh and logout endpoints. Every refresh rotates it; presenting a rotated
  one again revokes the whole session (reuse detection).
* ``rl_csrf``: the session's CSRF token, readable by the web app's JavaScript (not
  HttpOnly), echoed in ``X-CSRF-Token`` on mutating requests. It is also returned in the
  login/refresh response body for apps that cannot read the API's cookies.

``Secure`` is dropped only in ``RL_ENV=dev`` over plain ``http://localhost``, where
browsers would otherwise refuse the cookies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from rl_api import tokens
from rl_api.clientip import client_ip, user_agent
from rl_api.principal import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE
from rl_api.settings import Settings
from rl_db.admin import accounts

REFRESH_COOKIE_PATH = "/api/v1/auth"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    session_id: uuid.UUID
    access_expires_at: datetime
    refresh_expires_at: datetime
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


def cookie_secure(request: Request, settings: Settings) -> bool:
    if settings.env != "dev":
        return True
    return not (request.url.scheme == "http" and request.url.hostname in _LOCAL_HOSTS)


def _ttl(expires_at: datetime, now: datetime) -> int:
    return max(0, int((expires_at - now).total_seconds()))


def set_session_cookies(
    response: Response, request: Request, settings: Settings, issued: IssuedTokens
) -> None:
    now = datetime.now(UTC)
    secure = cookie_secure(request, settings)
    common = {"secure": secure, "samesite": "lax", "domain": settings.cookie_domain}
    response.set_cookie(
        ACCESS_COOKIE,
        issued.access_token,
        max_age=_ttl(issued.access_expires_at, now),
        httponly=True,
        path="/",
        **common,  # type: ignore[arg-type]
    )
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=_ttl(issued.refresh_expires_at, now),
        httponly=True,
        path=REFRESH_COOKIE_PATH,
        **common,  # type: ignore[arg-type]
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        max_age=_ttl(issued.refresh_expires_at, now),
        httponly=False,
        path="/",
        **common,  # type: ignore[arg-type]
    )
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookies(response: Response, request: Request, settings: Settings) -> None:
    secure = cookie_secure(request, settings)
    for name, path, httponly in (
        (ACCESS_COOKIE, "/", True),
        (REFRESH_COOKIE, REFRESH_COOKIE_PATH, True),
        (CSRF_COOKIE, "/", False),
    ):
        response.delete_cookie(
            name,
            path=path,
            domain=settings.cookie_domain,
            secure=secure,
            httponly=httponly,
            samesite="lax",
        )
    response.headers["Cache-Control"] = "no-store"


def _expiries(
    settings: Settings, now: datetime, session_expires_at: datetime
) -> tuple[datetime, datetime]:
    access = min(now + timedelta(seconds=settings.access_token_ttl_s), session_expires_at)
    refresh = min(now + timedelta(seconds=settings.refresh_token_ttl_s), session_expires_at)
    return access, refresh


async def start_session(
    db: AsyncSession,
    request: Request,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    auth_method: str,
) -> IssuedTokens:
    """Create a session (a new refresh-token family). The caller commits."""
    now = datetime.now(UTC)
    session_expires_at = now + timedelta(seconds=settings.session_max_age_s)
    access_expires_at, refresh_expires_at = _expiries(settings, now, session_expires_at)
    access = tokens.new_token(tokens.ACCESS)
    refresh = tokens.new_token(tokens.REFRESH)
    row = await accounts.create_auth_session(
        db,
        user_id=user_id,
        access_token_hash=tokens.hash_token(access),
        access_expires_at=access_expires_at,
        refresh_token_hash=tokens.hash_token(refresh),
        refresh_expires_at=refresh_expires_at,
        expires_at=session_expires_at,
        auth_method=auth_method,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    return IssuedTokens(
        session_id=row.id,
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
        access_token=access,
        refresh_token=refresh,
        csrf_token=tokens.csrf_token_for(request.app.state.csrf_key, row.id),
    )


async def rotate_session(
    db: AsyncSession,
    request: Request,
    settings: Settings,
    *,
    session_id: uuid.UUID,
    session_expires_at: datetime,
) -> IssuedTokens:
    """New access token + next refresh token of the chain. The caller has already claimed
    the presented refresh token (``mark_refresh_used``) and commits."""
    now = datetime.now(UTC)
    access_expires_at, refresh_expires_at = _expiries(settings, now, session_expires_at)
    access = tokens.new_token(tokens.ACCESS)
    refresh = tokens.new_token(tokens.REFRESH)
    await accounts.rotate_session(
        db,
        session_id=session_id,
        access_token_hash=tokens.hash_token(access),
        access_expires_at=access_expires_at,
        refresh_token_hash=tokens.hash_token(refresh),
        refresh_expires_at=refresh_expires_at,
        now=now,
    )
    return IssuedTokens(
        session_id=session_id,
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
        access_token=access,
        refresh_token=refresh,
        csrf_token=tokens.csrf_token_for(request.app.state.csrf_key, session_id),
    )
