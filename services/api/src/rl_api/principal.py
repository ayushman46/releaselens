"""Unified authentication and authorization (docs/security/AUTH.md §3-§5).

A request is authenticated by **either**

* an **API key** (``Authorization: Bearer rlk_…`` or ``x-releaselens-key``), which fixes the
  tenant and carries scopes (:mod:`rl_api.auth`), **or**
* a **browser session**: the ``rl_access`` cookie (httpOnly, opaque, 15 minutes). For
  methods other than GET/HEAD/OPTIONS the request must also pass the CSRF check
  (double-submit: ``X-CSRF-Token`` header equal to the ``rl_csrf`` cookie *and* to the
  session's HMAC-derived token). API-key requests are exempt: browsers never attach them
  on their own.

The tenant is never taken from the client. A session user names an organization in the
path (``/orgs/{org_id}/…``) or, for the tenant-implicit routes, the ``X-RL-Org`` header,
and that id is checked against the user's memberships. An org the caller cannot see answers
**404**, never 403, so the existence of another tenant's resources does not leak. Once the
caller is known to be a member, a too-low role answers 403.

Role matrix (``require_org(min_role, …)``): owner > admin > member > viewer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import tokens
from rl_api.auth import API_KEY_HEADER, authenticate
from rl_api.errors import ApiError
from rl_core.obs import bind_log_context
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.accounts import find_session_by_access_hash, get_membership_role
from rl_db.models import Role
from rl_db.repositories.audit import AuditActor

log = logging.getLogger("rl_api.principal")

ACCESS_COOKIE = "rl_access"
REFRESH_COOKIE = "rl_refresh"
CSRF_COOKIE = "rl_csrf"
CSRF_HEADER = "x-csrf-token"
ORG_HEADER = "x-rl-org"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

ROLE_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}


def role_at_least(role: Role, minimum: Role) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


@dataclass(frozen=True, slots=True)
class SessionUser:
    session_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling: exactly one of ``user`` (session) or ``api_key`` (key's context)."""

    user: SessionUser | None = None
    api_key: TenantContext | None = None

    def actor(self) -> AuditActor:
        if self.user is not None:
            return AuditActor(actor_type="user", user_id=self.user.user_id, email=self.user.email)
        if self.api_key is not None:
            return AuditActor(actor_type="api_key", api_key_id=self.api_key.actor_id)
        return AuditActor(actor_type="anonymous")


@dataclass(frozen=True, slots=True)
class OrgAccess:
    """A caller admitted to one organization."""

    ctx: TenantContext
    caller: Caller
    role: Role | None  # None for API keys (they carry scopes instead)

    @property
    def org_id(self) -> uuid.UUID:
        return self.ctx.tenant_id

    @property
    def user(self) -> SessionUser | None:
        return self.caller.user

    def actor(self) -> AuditActor:
        return self.caller.actor()


def _unauthorized(message: str = "authentication required") -> ApiError:
    return ApiError(401, "unauthorized", message)


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "not found")


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_sessions
    return factory


def _api_key_presented(request: Request) -> bool:
    return bool(request.headers.get("authorization") or request.headers.get(API_KEY_HEADER))


def check_csrf(request: Request, session_id: uuid.UUID) -> None:
    """Double-submit check bound to the session (raises 403 ``csrf_failed``)."""
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    expected = tokens.csrf_token_for(request.app.state.csrf_key, session_id)
    if not (
        tokens.constant_time_equal(header, cookie) and tokens.constant_time_equal(header, expected)
    ):
        raise ApiError(403, "csrf_failed", "missing or invalid CSRF token")


async def _session_user(request: Request) -> SessionUser | None:
    raw = request.cookies.get(ACCESS_COOKIE)
    if raw is None:
        return None
    if not tokens.well_formed(raw, tokens.ACCESS):
        raise _unauthorized("session expired or invalid")
    async with _sessions(request)() as session:
        found = await find_session_by_access_hash(
            session, tokens.hash_token(raw), now=datetime.now(UTC)
        )
    if found is None or not found.email_verified:
        raise _unauthorized("session expired or invalid")
    if request.method not in SAFE_METHODS:
        check_csrf(request, found.session_id)
    bind_log_context(user_id=str(found.user_id))
    return SessionUser(
        session_id=found.session_id,
        user_id=found.user_id,
        email=found.email,
        display_name=found.display_name,
    )


async def get_caller(request: Request, background: BackgroundTasks) -> Caller:
    """API key if one is presented (it wins, and a bad one is a 401 even with a valid
    cookie), else the session cookie, else 401."""
    if _api_key_presented(request):
        return Caller(api_key=await authenticate(request, background))
    user = await _session_user(request)
    if user is None:
        raise _unauthorized()
    return Caller(user=user)


CallerDep = Annotated[Caller, Depends(get_caller)]


async def require_user(caller: CallerDep) -> SessionUser:
    """Endpoints that act on behalf of a person (account, org membership, keys, billing)."""
    if caller.user is None:
        raise ApiError(403, "session_required", "this endpoint requires a signed-in user")
    return caller.user


async def _admit(
    request: Request,
    caller: Caller,
    org_id: uuid.UUID,
    *,
    min_role: Role,
    api_key_scope: str | None,
) -> OrgAccess:
    if caller.api_key is not None:
        if caller.api_key.tenant_id != org_id:
            raise _not_found()  # another tenant's org: indistinguishable from none
        if api_key_scope is None:
            raise ApiError(403, "session_required", "this endpoint requires a signed-in user")
        if not caller.api_key.has_scope(api_key_scope):
            raise ApiError(
                403, "insufficient_scope", f"this API key lacks the {api_key_scope!r} scope"
            )
        return OrgAccess(ctx=caller.api_key, caller=caller, role=None)
    assert caller.user is not None
    async with _sessions(request)() as session:
        role = await get_membership_role(session, tenant_id=org_id, user_id=caller.user.user_id)
    if role is None:
        raise _not_found()
    if not role_at_least(role, min_role):
        raise ApiError(403, "forbidden", f"this action requires the {min_role.value} role")
    ctx = TenantContext(
        tenant_id=org_id, principal=Principal.USER_SESSION, actor_id=caller.user.user_id
    )
    bind_log_context(tenant_id=str(org_id))
    request.state.tenant = ctx
    return OrgAccess(ctx=ctx, caller=caller, role=role)


def require_org(
    min_role: Role, *, api_key_scope: str | None = None
) -> Callable[..., Awaitable[OrgAccess]]:
    """Dependency factory for ``/orgs/{org_id}/…`` routes.

    ``api_key_scope``: the scope that lets an API key of this org in (``None``: sessions
    only). A key behaves like a viewer, so only read routes should accept one.
    """

    async def dependency(
        request: Request,
        caller: CallerDep,
        org_id: Annotated[uuid.UUID, Path(description="Organization (tenant) id")],
    ) -> OrgAccess:
        return await _admit(request, caller, org_id, min_role=min_role, api_key_scope=api_key_scope)

    dependency.__name__ = f"require_org_{min_role.value}"
    return dependency


async def require_tenant_read(request: Request, caller: CallerDep) -> TenantContext:
    """Tenant-implicit read routes (``/api/v1/deployments``): an API key with ``read``, or
    a session plus an ``X-RL-Org`` header naming one of the user's organizations."""
    if caller.api_key is not None:
        if not caller.api_key.has_scope("read"):
            raise ApiError(403, "insufficient_scope", "this API key lacks the 'read' scope")
        return caller.api_key
    raw = request.headers.get(ORG_HEADER)
    if not raw:
        raise ApiError(400, "org_required", f"send the organization id in {ORG_HEADER}")
    try:
        org_id = uuid.UUID(raw)
    except ValueError as exc:
        raise _not_found() from exc
    access = await _admit(request, caller, org_id, min_role=Role.VIEWER, api_key_scope="read")
    return access.ctx
