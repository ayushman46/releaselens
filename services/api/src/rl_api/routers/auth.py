"""Authentication: e-mail + password, verification, password reset, sessions (login,
refresh with rotation and reuse detection, logout) and GitHub OAuth.

Account-enumeration rules: signup, resend-verification and password-reset requests answer
the same ``202`` whether or not the address has an account, and do the same expensive work
(an Argon2 hash) either way; the e-mail itself is sent after the response. Login failures
are one ``401 invalid_credentials`` for "no such account", "no password" and "wrong
password". Every endpoint here is rate limited per client IP.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import audit as audit_log
from rl_api import github_oauth, tokens
from rl_api.deps import get_app_settings, get_hasher, get_http_client, get_mailer, get_sessions
from rl_api.errors import ApiError
from rl_api.mail import (
    Mailer,
    account_exists_email,
    password_reset_email,
    send_quietly,
    verification_email,
)
from rl_api.passwords import PasswordHasher, WeakPasswordError, check_password
from rl_api.principal import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    SessionUser,
    check_csrf,
    require_user,
)
from rl_api.ratelimit import enforce, limit_ip
from rl_api.sessions import (
    IssuedTokens,
    clear_session_cookies,
    cookie_secure,
    rotate_session,
    set_session_cookies,
    start_session,
)
from rl_api.settings import Settings
from rl_api.validation import EmailStr, Name
from rl_db.admin import accounts
from rl_db.models import AuthSession, TokenPurpose, User
from rl_db.repositories.audit import AuditActor

log = logging.getLogger("rl_api.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
OAUTH_STATE_TTL = timedelta(minutes=10)
OAUTH_STATE_COOKIE = "rl_oauth_state"
OAUTH_COOKIE_PATH = "/api/v1/auth/github"

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]
MailerDep = Annotated[Mailer, Depends(get_mailer)]
HasherDep = Annotated[PasswordHasher, Depends(get_hasher)]
RateLimited = Depends(limit_ip("auth"))


# ----------------------------------------------------------------------------- models
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    display_name: Name | None = None


class EmailIn(BaseModel):
    email: EmailStr


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=1024)


class Accepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    message: str


class StatusOut(BaseModel):
    status: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    email_verified: bool
    has_password: bool
    github_linked: bool


class SessionOut(BaseModel):
    """Login / refresh response. The tokens themselves are only in httpOnly cookies; the
    CSRF token is repeated here for clients that cannot read the API's cookies."""

    user: UserOut
    csrf_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified_at is not None,
        has_password=user.password_hash is not None,
        github_linked=user.github_user_id is not None,
    )


def _session_response(
    user: User, issued: IssuedTokens, request: Request, settings: Settings
) -> JSONResponse:
    body = SessionOut(
        user=user_out(user),
        csrf_token=issued.csrf_token,
        access_expires_at=issued.access_expires_at,
        refresh_expires_at=issued.refresh_expires_at,
    )
    response = JSONResponse(body.model_dump(mode="json"))
    set_session_cookies(response, request, settings, issued)
    return response


def _weak(exc: WeakPasswordError) -> ApiError:
    return ApiError(
        422,
        "weak_password",
        "password does not meet the policy",
        details={"reasons": exc.reasons},
    )


async def _issue_reset(db: AsyncSession, user: User, settings: Settings, now: datetime) -> str:
    token = tokens.new_token(tokens.RESET_PASSWORD)
    await accounts.issue_user_token(
        db,
        user_id=user.id,
        purpose=TokenPurpose.RESET_PASSWORD,
        token_hash=tokens.hash_token(token),
        expires_at=now + RESET_TTL,
        now=now,
    )
    return settings.web_url(f"/reset-password#token={token}")


async def _issue_verification(
    db: AsyncSession, user: User, settings: Settings, now: datetime
) -> str:
    token = tokens.new_token(tokens.VERIFY_EMAIL)
    await accounts.issue_user_token(
        db,
        user_id=user.id,
        purpose=TokenPurpose.VERIFY_EMAIL,
        token_hash=tokens.hash_token(token),
        expires_at=now + VERIFY_TTL,
        now=now,
    )
    return settings.web_url(f"/verify-email#token={token}")


SIGNUP_MESSAGE = "If this address can be used, a verification link is on its way."


# ----------------------------------------------------------------------------- signup & verify
@router.post(
    "/signup",
    status_code=202,
    response_model=Accepted,
    summary="Create an account (sends a verification e-mail)",
    dependencies=[RateLimited],
    responses={422: {"description": "Invalid e-mail or weak password"}, 429: {}},
)
async def signup(
    body: SignupIn,
    request: Request,
    background: BackgroundTasks,
    settings: SettingsDep,
    sessions: SessionsDep,
    mailer: MailerDep,
    hasher: HasherDep,
) -> Accepted:
    try:
        check_password(body.password, email=body.email)
    except WeakPasswordError as exc:
        raise _weak(exc) from exc
    # Hash before knowing whether the account exists: both paths cost the same.
    password_hash = await hasher.hash(body.password)
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        existing = await accounts.get_user_by_email(db, body.email)
        if existing is not None:
            # Never touch an existing account from here (no password overwrite, even when
            # unverified). The owner gets a notice with a reset link instead.
            link = await _issue_reset(db, existing, settings, now)
            email = account_exists_email(existing.email, link)
        else:
            user = await accounts.create_user(
                db, email=body.email, password_hash=password_hash, display_name=body.display_name
            )
            link = await _issue_verification(db, user, settings, now)
            email = verification_email(user.email, link)
            await audit_log.security_event(
                db,
                request,
                audit_log.SIGNED_UP,
                actor=AuditActor(actor_type="user", user_id=user.id, email=user.email),
            )
    background.add_task(send_quietly, mailer, email)
    return Accepted(message=SIGNUP_MESSAGE)


@router.post(
    "/verify-email",
    response_model=StatusOut,
    summary="Confirm an e-mail address with the e-mailed token",
    dependencies=[RateLimited],
    responses={400: {"description": "Invalid, used or expired token"}, 429: {}},
)
async def verify_email(body: TokenIn, request: Request, sessions: SessionsDep) -> StatusOut:
    if not tokens.well_formed(body.token, tokens.VERIFY_EMAIL):
        raise ApiError(400, "invalid_token", "the link is invalid or has expired")
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        user_id = await accounts.consume_user_token(
            db, purpose=TokenPurpose.VERIFY_EMAIL, token_hash=tokens.hash_token(body.token), now=now
        )
        if user_id is None:
            raise ApiError(400, "invalid_token", "the link is invalid or has expired")
        user = await accounts.get_user(db, user_id)
        if user is not None and user.email_verified_at is None:
            user.email_verified_at = now
            await audit_log.security_event(
                db,
                request,
                audit_log.EMAIL_VERIFIED,
                actor=AuditActor(actor_type="user", user_id=user.id, email=user.email),
            )
    return StatusOut(status="verified")


@router.post(
    "/verify-email/resend",
    status_code=202,
    response_model=Accepted,
    summary="Send a new verification e-mail",
    dependencies=[RateLimited],
    responses={429: {}},
)
async def resend_verification(
    body: EmailIn,
    background: BackgroundTasks,
    settings: SettingsDep,
    sessions: SessionsDep,
    mailer: MailerDep,
) -> Accepted:
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        user = await accounts.get_user_by_email(db, body.email)
        if user is not None and user.email_verified_at is None:
            link = await _issue_verification(db, user, settings, now)
            background.add_task(send_quietly, mailer, verification_email(user.email, link))
    return Accepted(message=SIGNUP_MESSAGE)


# ----------------------------------------------------------------------------- login & sessions
INVALID_CREDENTIALS = "invalid e-mail or password"


@router.post(
    "/login",
    response_model=SessionOut,
    summary="Sign in with e-mail and password (sets session cookies)",
    dependencies=[RateLimited],
    responses={
        401: {"description": "Invalid e-mail or password"},
        403: {"description": "E-mail address not verified yet"},
        429: {},
    },
)
async def login(
    body: LoginIn,
    request: Request,
    settings: SettingsDep,
    sessions: SessionsDep,
    hasher: HasherDep,
) -> JSONResponse:
    from rl_api.clientip import client_ip

    # Slow down guessing against one account from one address.
    await enforce(
        request,
        settings.rate_policy("login_account"),
        f"{audit_log.email_fingerprint(body.email)}:{client_ip(request)}",
    )
    async with sessions() as db:
        user = await accounts.get_user_by_email(db, body.email)
    ok = await hasher.verify(user.password_hash if user else None, body.password)
    if user is None or not ok:
        async with sessions.begin() as db:
            await audit_log.security_event(
                db,
                request,
                audit_log.LOGIN_FAILED,
                actor=AuditActor(actor_type="anonymous"),
                details={
                    "reason": "invalid_credentials",
                    "email_fingerprint": audit_log.email_fingerprint(body.email),
                },
            )
        raise ApiError(401, "invalid_credentials", INVALID_CREDENTIALS)
    if user.email_verified_at is None:
        raise ApiError(403, "email_not_verified", "confirm your e-mail address first")

    assert user.password_hash is not None
    new_hash = await hasher.hash(body.password) if hasher.needs_rehash(user.password_hash) else None
    async with sessions.begin() as db:
        if new_hash is not None:
            await accounts.set_password_hash(db, user.id, new_hash)
        issued = await start_session(db, request, settings, user_id=user.id, auth_method="password")
        await audit_log.security_event(
            db,
            request,
            audit_log.LOGIN_SUCCEEDED,
            actor=AuditActor(actor_type="user", user_id=user.id, email=user.email),
            details={
                "method": "password",
                "session_id": str(issued.session_id),
                "rehashed": new_hash is not None,
            },
        )
    return _session_response(user, issued, request, settings)


@router.post(
    "/refresh",
    response_model=SessionOut,
    summary="Rotate the session's tokens (refresh cookie + CSRF header)",
    responses={
        401: {"description": "Missing, expired, revoked or reused refresh token"},
        403: {"description": "CSRF check failed"},
    },
)
async def refresh(request: Request, settings: SettingsDep, sessions: SessionsDep) -> JSONResponse:
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw is None or not tokens.well_formed(raw, tokens.REFRESH):
        raise ApiError(401, "unauthorized", "no valid refresh token")
    now = datetime.now(UTC)
    token_hash = tokens.hash_token(raw)
    async with sessions() as db:
        presented = await accounts.find_refresh_token(db, token_hash)
    if presented is None or presented.session_revoked_at is not None:
        raise ApiError(401, "unauthorized", "session expired; sign in again")
    if presented.token_used_at is not None:
        return await _reuse_detected(request, settings, sessions, presented, now)
    if presented.token_expires_at <= now or presented.session_expires_at <= now:
        raise ApiError(401, "unauthorized", "session expired; sign in again")
    # CSRF before the token is consumed: a failed check must not burn the user's token.
    check_csrf(request, presented.session_id)
    user: User | None = None
    issued: IssuedTokens | None = None
    async with sessions.begin() as db:
        claimed = await accounts.mark_refresh_used(db, presented.token_id, now=now)
        if claimed:
            issued = await rotate_session(
                db,
                request,
                settings,
                session_id=presented.session_id,
                session_expires_at=presented.session_expires_at,
            )
            user = await accounts.get_user(db, presented.user_id)
    if not claimed:
        # Another request rotated this token between our read and our claim.
        return await _reuse_detected(request, settings, sessions, presented, now)
    if user is None or issued is None:
        raise ApiError(401, "unauthorized", "session expired; sign in again")
    return _session_response(user, issued, request, settings)


async def _reuse_detected(
    request: Request,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    presented: accounts.PresentedRefresh,
    now: datetime,
) -> JSONResponse:
    """A rotated refresh token came back, so someone else holds a copy of the chain:
    revoke the whole session (every token of the family) and make the caller sign in."""
    async with sessions.begin() as db:
        await accounts.revoke_session(
            db, presented.session_id, reason="refresh_token_reuse", now=now
        )
        await audit_log.security_event(
            db,
            request,
            audit_log.REFRESH_REUSE,
            actor=AuditActor(actor_type="user", user_id=presented.user_id),
            details={"session_id": str(presented.session_id)},
        )
    log.warning(
        "refresh token reuse; session revoked", extra={"session_id": str(presented.session_id)}
    )
    response = JSONResponse(
        {
            "error": {
                "code": "refresh_token_reused",
                "message": "this session was revoked; sign in again",
                "request_id": getattr(request.state, "request_id", None),
            }
        },
        status_code=401,
    )
    clear_session_cookies(response, request, settings)
    return response


@router.post(
    "/logout",
    status_code=204,
    summary="Revoke the current session and clear its cookies",
    responses={403: {"description": "CSRF check failed"}},
)
async def logout(request: Request, settings: SettingsDep, sessions: SessionsDep) -> Response:
    now = datetime.now(UTC)
    session_id: uuid.UUID | None = None
    async with sessions() as db:
        access = request.cookies.get(ACCESS_COOKIE)
        if tokens.well_formed(access, tokens.ACCESS) and access is not None:
            session_id = await accounts.find_session_id_by_access_hash(
                db, tokens.hash_token(access)
            )
        refresh_raw = request.cookies.get(REFRESH_COOKIE)
        if session_id is None and tokens.well_formed(refresh_raw, tokens.REFRESH):
            assert refresh_raw is not None
            found = await accounts.find_refresh_token(db, tokens.hash_token(refresh_raw))
            if found is not None and found.session_revoked_at is None:
                session_id = found.session_id
    if session_id is not None:
        check_csrf(request, session_id)
        async with sessions.begin() as db:
            row = await db.get(AuthSession, session_id)
            await accounts.revoke_session(db, session_id, reason="logout", now=now)
            await audit_log.security_event(
                db,
                request,
                audit_log.LOGOUT,
                actor=AuditActor(actor_type="user", user_id=row.user_id if row else None),
                details={"session_id": str(session_id)},
            )
    response = Response(status_code=204)
    clear_session_cookies(response, request, settings)
    return response


# ----------------------------------------------------------------------------- password reset
RESET_MESSAGE = "If this address has an account, a reset link is on its way."


@router.post(
    "/password-reset/request",
    status_code=202,
    response_model=Accepted,
    summary="E-mail a password-reset link",
    dependencies=[RateLimited],
    responses={429: {}},
)
async def request_password_reset(
    body: EmailIn,
    background: BackgroundTasks,
    settings: SettingsDep,
    sessions: SessionsDep,
    mailer: MailerDep,
) -> Accepted:
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        user = await accounts.get_user_by_email(db, body.email)
        if user is not None:
            link = await _issue_reset(db, user, settings, now)
            background.add_task(send_quietly, mailer, password_reset_email(user.email, link))
    return Accepted(message=RESET_MESSAGE)


@router.post(
    "/password-reset/confirm",
    response_model=StatusOut,
    summary="Set a new password with the e-mailed token (signs out every session)",
    dependencies=[RateLimited],
    responses={400: {"description": "Invalid, used or expired token"}, 422: {}, 429: {}},
)
async def confirm_password_reset(
    body: ResetConfirmIn, request: Request, sessions: SessionsDep, hasher: HasherDep
) -> StatusOut:
    if not tokens.well_formed(body.token, tokens.RESET_PASSWORD):
        raise ApiError(400, "invalid_token", "the link is invalid or has expired")
    now = datetime.now(UTC)
    password_hash: str | None = None
    async with sessions.begin() as db:
        user_id = await accounts.consume_user_token(
            db,
            purpose=TokenPurpose.RESET_PASSWORD,
            token_hash=tokens.hash_token(body.token),
            now=now,
        )
        user = await accounts.get_user(db, user_id) if user_id else None
        if user is None:
            raise ApiError(400, "invalid_token", "the link is invalid or has expired")
        try:
            check_password(body.new_password, email=user.email)
        except WeakPasswordError as exc:
            # Raising rolls the transaction back, so the token stays usable for a retry.
            raise _weak(exc) from exc
        password_hash = await hasher.hash(body.new_password)
        user.password_hash = password_hash
        # The link reached the inbox, which proves control of the address.
        if user.email_verified_at is None:
            user.email_verified_at = now
        await accounts.retire_user_tokens(
            db, user_id=user.id, purpose=TokenPurpose.RESET_PASSWORD, now=now
        )
        revoked = await accounts.revoke_user_sessions(db, user.id, reason="password_reset", now=now)
        await audit_log.security_event(
            db,
            request,
            audit_log.PASSWORD_RESET,
            actor=AuditActor(actor_type="user", user_id=user.id, email=user.email),
            details={"sessions_revoked": revoked},
        )
    return StatusOut(status="password_updated")


# ----------------------------------------------------------------------------- GitHub OAuth
def _oauth_not_configured() -> ApiError:
    return ApiError(503, "github_oauth_not_configured", "sign-in with GitHub is not configured")


class GitHubOAuthStatus(BaseModel):
    configured: bool


@router.get(
    "/github/status",
    response_model=GitHubOAuthStatus,
    summary="Whether sign-in with GitHub is configured (no side effects)",
)
async def github_status(settings: SettingsDep) -> GitHubOAuthStatus:
    """Lets the login page show or hide the GitHub button. Reads configuration only: no
    session, state, cookie or rate-limit bucket is touched."""
    return GitHubOAuthStatus(configured=settings.github_oauth_configured)


@router.get(
    "/github/authorize",
    status_code=302,
    summary="Start sign-in with GitHub (redirects to GitHub; state + PKCE)",
    dependencies=[RateLimited],
    responses={302: {"description": "Redirect to GitHub"}, 503: {"description": "Not configured"}},
)
async def github_authorize(
    request: Request, settings: SettingsDep, sessions: SessionsDep
) -> RedirectResponse:
    if not settings.github_oauth_configured:
        raise _oauth_not_configured()
    assert settings.github_oauth_client_id is not None
    state = tokens.new_token(tokens.OAUTH_STATE)
    verifier, challenge = tokens.pkce_pair()
    async with sessions.begin() as db:
        await accounts.store_oauth_state(
            db,
            state_hash=tokens.hash_token(state),
            code_verifier=verifier,
            expires_at=datetime.now(UTC) + OAUTH_STATE_TTL,
        )
    url = github_oauth.authorize_url(
        client_id=settings.github_oauth_client_id,
        redirect_uri=settings.github_redirect_uri(),
        state=state,
        code_challenge=challenge,
    )
    response = RedirectResponse(url, status_code=302)
    # Binds the flow to this browser: a callback link forged by someone else fails.
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=int(OAUTH_STATE_TTL.total_seconds()),
        httponly=True,
        secure=cookie_secure(request, settings),
        samesite="lax",
        path=OAUTH_COOKIE_PATH,
        domain=settings.cookie_domain,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _login_error(settings: Settings, request: Request, code: str) -> RedirectResponse:
    response = RedirectResponse(settings.web_url(f"/login?error={code}"), status_code=302)
    response.delete_cookie(
        OAUTH_STATE_COOKIE, path=OAUTH_COOKIE_PATH, domain=settings.cookie_domain
    )
    return response


async def _github_user(
    db: AsyncSession, identity: github_oauth.GitHubIdentity, now: datetime
) -> tuple[User, bool]:
    """Find or create the account for a GitHub identity. Returns (user, newly_linked)."""
    user = await accounts.get_user_by_github_id(db, identity.id)
    if user is not None:
        return user, False
    user = await accounts.get_user_by_email(db, identity.email)
    if user is None:
        user = await accounts.create_user(
            db,
            email=identity.email,
            password_hash=None,
            display_name=identity.name or identity.login or None,
            email_verified_at=now,
            github_user_id=identity.id,
        )
        return user, True
    if user.email_verified_at is None:
        # Nobody ever proved control of this address. A password set by whoever signed up
        # with it must not survive the real owner arriving through GitHub (account
        # pre-hijacking), so it is dropped; the owner can set one by password reset.
        user.password_hash = None
        user.email_verified_at = now
    user.github_user_id = identity.id
    return user, True


@router.get(
    "/github/callback",
    status_code=302,
    summary="GitHub OAuth callback (exchanges the code, signs in, redirects to the app)",
    dependencies=[RateLimited],
    responses={
        302: {"description": "Redirect to the web app"},
        503: {"description": "Not configured"},
    },
)
async def github_callback(
    request: Request,
    settings: SettingsDep,
    sessions: SessionsDep,
    http: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    code: Annotated[str | None, Query(max_length=512)] = None,
    state: Annotated[str | None, Query(max_length=128)] = None,
    error: Annotated[str | None, Query(max_length=128)] = None,
) -> RedirectResponse:
    if not settings.github_oauth_configured:
        raise _oauth_not_configured()
    assert settings.github_oauth_client_id is not None
    assert settings.github_oauth_client_secret is not None
    if error:
        return _login_error(settings, request, "github_denied")
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if (
        not code
        or not tokens.well_formed(state, tokens.OAUTH_STATE)
        or not tokens.constant_time_equal(state, cookie_state)
    ):
        return _login_error(settings, request, "oauth_state_mismatch")
    assert state is not None
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        verifier = await accounts.consume_oauth_state(
            db, state_hash=tokens.hash_token(state), now=now
        )
    if verifier is None:
        return _login_error(settings, request, "oauth_state_expired")
    try:
        access_token = await github_oauth.exchange_code(
            http,
            client_id=settings.github_oauth_client_id,
            client_secret=settings.github_oauth_client_secret.get_secret_value(),
            code=code,
            redirect_uri=settings.github_redirect_uri(),
            code_verifier=verifier,
        )
        identity = await github_oauth.fetch_identity(http, access_token)
    except github_oauth.GitHubOAuthError as exc:
        log.info("github sign-in failed", extra={"code": exc.code})
        return _login_error(settings, request, exc.code)

    async with sessions.begin() as db:
        user, linked = await _github_user(db, identity, now)
        issued = await start_session(db, request, settings, user_id=user.id, auth_method="github")
        actor = AuditActor(actor_type="user", user_id=user.id, email=user.email)
        if linked:
            await audit_log.security_event(
                db,
                request,
                audit_log.GITHUB_LINKED,
                actor=actor,
                details={"github_login": identity.login},
            )
        await audit_log.security_event(
            db,
            request,
            audit_log.LOGIN_SUCCEEDED,
            actor=actor,
            details={"method": "github", "session_id": str(issued.session_id)},
        )
    response = RedirectResponse(settings.web_url("/app"), status_code=302)
    set_session_cookies(response, request, settings, issued)
    response.delete_cookie(
        OAUTH_STATE_COOKIE, path=OAUTH_COOKIE_PATH, domain=settings.cookie_domain
    )
    return response


# ----------------------------------------------------------------------------- me
me_router = APIRouter(prefix="/api/v1", tags=["auth"])


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    name: str
    slug: str
    plan: str
    role: str


class MeOut(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]


@me_router.get("/me", response_model=MeOut, summary="The signed-in user and their organizations")
async def me(user: Annotated[SessionUser, Depends(require_user)], sessions: SessionsDep) -> MeOut:
    async with sessions() as db:
        row = await accounts.get_user(db, user.user_id)
        memberships = await accounts.list_user_memberships(db, user.user_id)
    if row is None:
        raise ApiError(401, "unauthorized", "session expired or invalid")
    return MeOut(
        user=user_out(row),
        memberships=[
            MembershipOut(
                org_id=m.tenant_id, name=m.org_name, slug=m.org_slug, plan=m.plan, role=m.role.value
            )
            for m in memberships
        ],
    )
