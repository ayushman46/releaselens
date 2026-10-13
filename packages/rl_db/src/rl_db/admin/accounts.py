"""Users, login sessions, one-time tokens and OAuth state (global, so no TenantContext).

A user is not owned by any tenant: authentication runs *before* a tenant is known and is
how the caller's memberships (and so their tenants) are found. Everything here is keyed by
user id or by the hash of a secret the caller presented.

All secrets arrive here already hashed (``sha256`` hex); plaintext tokens never reach this
layer. Functions never commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_db.models.accounts import AuthSession, OAuthState, RefreshToken, UserToken
from rl_db.models.identity import Invitation, Membership, Organization, Role, User
from rl_db.repositories._tenancy import cross_tenant

# ----------------------------------------------------------------------------- users


def normalize_email(email: str) -> str:
    """Canonical form used for lookups (case-insensitive, trimmed)."""
    return email.strip().lower()


@cross_tenant
async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


@cross_tenant
async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == normalize_email(email))
    return (await session.execute(stmt)).scalar_one_or_none()


@cross_tenant
async def get_user_by_github_id(session: AsyncSession, github_user_id: int) -> User | None:
    stmt = select(User).where(User.github_user_id == github_user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@cross_tenant
async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str | None,
    display_name: str | None = None,
    email_verified_at: datetime | None = None,
    github_user_id: int | None = None,
) -> User:
    user = User(
        id=uuid7(),
        email=normalize_email(email),
        password_hash=password_hash,
        display_name=display_name,
        email_verified_at=email_verified_at,
        github_user_id=github_user_id,
    )
    session.add(user)
    await session.flush()
    return user


@cross_tenant
async def set_password_hash(session: AsyncSession, user_id: uuid.UUID, password_hash: str) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(password_hash=password_hash)
    )


# ----------------------------------------------------------------------------- one-time tokens


@cross_tenant
async def issue_user_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    purpose: str,
    token_hash: str,
    expires_at: datetime,
    now: datetime,
) -> None:
    """Store a new single-use token. Earlier unused tokens of the same purpose are retired
    first, so only the most recent e-mail's link works."""
    await session.execute(
        update(UserToken)
        .where(
            UserToken.user_id == user_id,
            UserToken.purpose == purpose,
            UserToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    session.add(
        UserToken(
            id=uuid7(),
            user_id=user_id,
            purpose=purpose,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    await session.flush()


@cross_tenant
async def consume_user_token(
    session: AsyncSession, *, purpose: str, token_hash: str, now: datetime
) -> uuid.UUID | None:
    """Mark the token used and return its user id, or None if it is unknown, of another
    purpose, expired or already used. One UPDATE, so two concurrent uses cannot both win."""
    stmt = (
        update(UserToken)
        .where(
            UserToken.token_hash == token_hash,
            UserToken.purpose == purpose,
            UserToken.used_at.is_(None),
            UserToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(UserToken.user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@cross_tenant
async def retire_user_tokens(
    session: AsyncSession, *, user_id: uuid.UUID, purpose: str, now: datetime
) -> None:
    await session.execute(
        update(UserToken)
        .where(
            UserToken.user_id == user_id,
            UserToken.purpose == purpose,
            UserToken.used_at.is_(None),
        )
        .values(used_at=now)
    )


# ----------------------------------------------------------------------------- sessions


@cross_tenant
async def create_auth_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    access_token_hash: str,
    access_expires_at: datetime,
    refresh_token_hash: str,
    refresh_expires_at: datetime,
    expires_at: datetime,
    auth_method: str,
    ip: str | None,
    user_agent: str | None,
) -> AuthSession:
    row = AuthSession(
        id=uuid7(),
        user_id=user_id,
        access_token_hash=access_token_hash,
        access_expires_at=access_expires_at,
        expires_at=expires_at,
        auth_method=auth_method,
        ip=ip,
        user_agent=user_agent,
    )
    session.add(row)
    await session.flush()
    session.add(
        RefreshToken(
            id=uuid7(),
            session_id=row.id,
            token_hash=refresh_token_hash,
            expires_at=refresh_expires_at,
        )
    )
    await session.flush()
    return row


@dataclass(frozen=True, slots=True)
class SessionUser:
    """What authenticating a request needs: the session and its (verified) user."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str | None
    email_verified: bool
    access_expires_at: datetime


@cross_tenant
async def find_session_by_access_hash(
    session: AsyncSession, access_token_hash: str, *, now: datetime
) -> SessionUser | None:
    """The live session holding this access token: not revoked, not expired."""
    stmt = (
        select(
            AuthSession.id,
            AuthSession.user_id,
            AuthSession.access_expires_at,
            User.email,
            User.display_name,
            User.email_verified_at,
        )
        .join(User, User.id == AuthSession.user_id)
        .where(
            AuthSession.access_token_hash == access_token_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.access_expires_at > now,
            AuthSession.expires_at > now,
        )
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return SessionUser(
        session_id=row.id,
        user_id=row.user_id,
        email=row.email,
        display_name=row.display_name,
        email_verified=row.email_verified_at is not None,
        access_expires_at=row.access_expires_at,
    )


@cross_tenant
async def find_session_id_by_access_hash(
    session: AsyncSession, access_token_hash: str
) -> uuid.UUID | None:
    """Session of an access token whether or not it expired (logout must work with a stale
    access cookie)."""
    stmt = select(AuthSession.id).where(
        AuthSession.access_token_hash == access_token_hash, AuthSession.revoked_at.is_(None)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class PresentedRefresh:
    token_id: uuid.UUID
    session_id: uuid.UUID
    user_id: uuid.UUID
    token_expires_at: datetime
    token_used_at: datetime | None
    session_expires_at: datetime
    session_revoked_at: datetime | None


@cross_tenant
async def find_refresh_token(
    session: AsyncSession, refresh_token_hash: str
) -> PresentedRefresh | None:
    stmt = (
        select(
            RefreshToken.id,
            RefreshToken.session_id,
            RefreshToken.expires_at,
            RefreshToken.used_at,
            AuthSession.user_id,
            AuthSession.expires_at.label("session_expires_at"),
            AuthSession.revoked_at,
        )
        .join(AuthSession, AuthSession.id == RefreshToken.session_id)
        .where(RefreshToken.token_hash == refresh_token_hash)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return PresentedRefresh(
        token_id=row.id,
        session_id=row.session_id,
        user_id=row.user_id,
        token_expires_at=row.expires_at,
        token_used_at=row.used_at,
        session_expires_at=row.session_expires_at,
        session_revoked_at=row.revoked_at,
    )


@cross_tenant
async def mark_refresh_used(session: AsyncSession, token_id: uuid.UUID, *, now: datetime) -> bool:
    """Claim a refresh token for rotation. False if it was already used: a concurrent
    rotation won, and this presentation is a reuse."""
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.id == token_id, RefreshToken.used_at.is_(None))
        .values(used_at=now)
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def rotate_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    access_token_hash: str,
    access_expires_at: datetime,
    refresh_token_hash: str,
    refresh_expires_at: datetime,
    now: datetime,
) -> None:
    """Replace the session's access token and append the next refresh token of the chain."""
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id)
        .values(
            access_token_hash=access_token_hash,
            access_expires_at=access_expires_at,
            last_refreshed_at=now,
        )
    )
    session.add(
        RefreshToken(
            id=uuid7(),
            session_id=session_id,
            token_hash=refresh_token_hash,
            expires_at=refresh_expires_at,
        )
    )
    await session.flush()


@cross_tenant
async def revoke_session(
    session: AsyncSession, session_id: uuid.UUID, *, reason: str, now: datetime
) -> bool:
    stmt = (
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


@cross_tenant
async def revoke_user_sessions(
    session: AsyncSession, user_id: uuid.UUID, *, reason: str, now: datetime
) -> int:
    stmt = (
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0))


# ----------------------------------------------------------------------------- OAuth state


@cross_tenant
async def store_oauth_state(
    session: AsyncSession, *, state_hash: str, code_verifier: str, expires_at: datetime
) -> None:
    session.add(
        OAuthState(state_hash=state_hash, code_verifier=code_verifier, expires_at=expires_at)
    )
    await session.flush()


@cross_tenant
async def consume_oauth_state(
    session: AsyncSession, *, state_hash: str, now: datetime
) -> str | None:
    """Delete the state and return its PKCE verifier; None if unknown or expired."""
    stmt = (
        delete(OAuthState)
        .where(OAuthState.state_hash == state_hash)
        .returning(OAuthState.code_verifier, OAuthState.expires_at)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None or row.expires_at <= now:
        return None
    return str(row.code_verifier)


# ----------------------------------------------------------------------------- memberships


@dataclass(frozen=True, slots=True)
class UserMembership:
    tenant_id: uuid.UUID
    org_name: str
    org_slug: str
    plan: str
    role: Role


@cross_tenant
async def list_user_memberships(session: AsyncSession, user_id: uuid.UUID) -> list[UserMembership]:
    """The organizations a user belongs to. It reads across tenants by design: it is how a
    signed-in user discovers which tenants they may select."""
    stmt = (
        select(
            Organization.id,
            Organization.name,
            Organization.slug,
            Organization.plan,
            Membership.role,
        )
        .join(Membership, Membership.tenant_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.name, Organization.id)
    )
    return [
        UserMembership(
            tenant_id=r.id, org_name=r.name, org_slug=r.slug, plan=str(r.plan), role=Role(r.role)
        )
        for r in (await session.execute(stmt)).all()
    ]


@cross_tenant
async def get_membership_role(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> Role | None:
    """The user's role in ``tenant_id``, or None if they are not a member. This is the check
    that turns a client-supplied org id into a trusted TenantContext."""
    stmt = select(Membership.role).where(
        Membership.tenant_id == tenant_id, Membership.user_id == user_id
    )
    role = (await session.execute(stmt)).scalar_one_or_none()
    return Role(role) if role is not None else None


# ----------------------------------------------------------------------------- invitations


@cross_tenant
async def find_invitation_by_token(
    session: AsyncSession, token_hash: str, *, for_update: bool = False
) -> Invitation | None:
    """An invitation by its secret token. The token identifies the tenant, so this runs
    before any TenantContext exists."""
    stmt = select(Invitation).where(Invitation.token_hash == token_hash)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()
