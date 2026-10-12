"""Organization settings, members and invitations (tenant-scoped, ADR-0010)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.admin.accounts import normalize_email
from rl_db.models.identity import Invitation, Membership, Organization, Role, User
from rl_db.repositories._tenancy import require_tenant


async def get_organization(
    session: AsyncSession, ctx: TenantContext, *, for_update: bool = False
) -> Organization | None:
    """The caller's own organization. ``for_update`` locks the row, which serialises
    limit checks and member changes of one tenant (e.g. "is this the last owner?")."""
    stmt = select(Organization).where(Organization.id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def rename_organization(session: AsyncSession, ctx: TenantContext, name: str) -> None:
    await session.execute(
        update(Organization).where(Organization.id == ctx.tenant_id).values(name=name)
    )


async def delete_organization(session: AsyncSession, ctx: TenantContext) -> bool:
    """Delete the tenant and, through ``ON DELETE CASCADE``, everything it owns. The audit
    log is kept on purpose (it has no foreign key to organizations)."""
    result = await session.execute(delete(Organization).where(Organization.id == ctx.tenant_id))
    return bool(getattr(result, "rowcount", 0))


# ----------------------------------------------------------------------------- members


@dataclass(frozen=True, slots=True)
class MemberRow:
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: Role
    joined_at: datetime


async def add_member(
    session: AsyncSession, ctx: TenantContext, *, user_id: uuid.UUID, role: Role
) -> None:
    session.add(Membership(id=uuid7(), tenant_id=ctx.tenant_id, user_id=user_id, role=role))
    await session.flush()


async def list_members(session: AsyncSession, ctx: TenantContext) -> list[MemberRow]:
    stmt = (
        select(User.id, User.email, User.display_name, Membership.role, Membership.created_at)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == ctx.tenant_id)
        .order_by(Membership.created_at, User.id)
    )
    return [
        MemberRow(
            user_id=r.id,
            email=r.email,
            display_name=r.display_name,
            role=Role(r.role),
            joined_at=r.created_at,
        )
        for r in (await session.execute(stmt)).all()
    ]


async def get_member_role(
    session: AsyncSession, ctx: TenantContext, user_id: uuid.UUID
) -> Role | None:
    stmt = select(Membership.role).where(
        Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id
    )
    role = (await session.execute(stmt)).scalar_one_or_none()
    return Role(role) if role is not None else None


async def count_owners(session: AsyncSession, ctx: TenantContext) -> int:
    stmt = select(func.count()).where(
        Membership.tenant_id == ctx.tenant_id, Membership.role == Role.OWNER
    )
    return int((await session.execute(stmt)).scalar_one())


async def set_member_role(
    session: AsyncSession, ctx: TenantContext, user_id: uuid.UUID, role: Role
) -> bool:
    result = await session.execute(
        update(Membership)
        .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id)
        .values(role=role)
    )
    return bool(getattr(result, "rowcount", 0))


async def remove_member(session: AsyncSession, ctx: TenantContext, user_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(Membership).where(
            Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def is_member_email(session: AsyncSession, ctx: TenantContext, email: str) -> bool:
    stmt = (
        select(func.count())
        .select_from(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == ctx.tenant_id, func.lower(User.email) == normalize_email(email)
        )
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


# ----------------------------------------------------------------------------- invitations


async def create_invitation(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    email: str,
    role: Role,
    token_hash: str,
    invited_by: uuid.UUID | None,
    expires_at: datetime,
    now: datetime,
) -> Invitation:
    """A new invitation. Pending invitations to the same address are withdrawn first, so
    only the latest e-mailed link works."""
    normalized = normalize_email(email)
    await session.execute(
        update(Invitation)
        .where(
            Invitation.tenant_id == ctx.tenant_id,
            func.lower(Invitation.email) == normalized,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    row = Invitation(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        email=normalized,
        role=role,
        token_hash=token_hash,
        invited_by=invited_by,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def list_pending_invitations(
    session: AsyncSession, ctx: TenantContext, *, now: datetime
) -> list[Invitation]:
    stmt = (
        select(Invitation)
        .where(
            Invitation.tenant_id == ctx.tenant_id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
        .order_by(Invitation.created_at.desc(), Invitation.id.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def revoke_invitation(
    session: AsyncSession, ctx: TenantContext, invitation_id: uuid.UUID, *, now: datetime
) -> Invitation | None:
    stmt = (
        update(Invitation)
        .where(
            Invitation.tenant_id == ctx.tenant_id,
            Invitation.id == invitation_id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(Invitation)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_invitation_accepted(
    session: AsyncSession,
    ctx: TenantContext,
    invitation: Invitation,
    *,
    user_id: uuid.UUID,
    now: datetime,
) -> None:
    require_tenant(ctx, invitation.tenant_id)
    await session.execute(
        update(Invitation)
        .where(Invitation.tenant_id == ctx.tenant_id, Invitation.id == invitation.id)
        .values(accepted_at=now, accepted_by=user_id)
    )
