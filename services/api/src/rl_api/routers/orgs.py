"""Organizations (tenants), members and invitations.

Role rules (docs/security/AUTH.md §5):

* any member may read the organization and its member list;
* admins rename the organization, invite, revoke invitations, change roles and remove
  members, but only an **owner** may grant, change or remove the owner role;
* the **last owner** can be neither demoted nor removed (409 ``last_owner``);
* anyone may leave an organization (remove themselves), except its last owner;
* only an owner deletes the organization, and not while a paid subscription is live.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import audit as audit_log
from rl_api import tokens
from rl_api.deps import get_app_settings, get_mailer, get_sessions
from rl_api.errors import ApiError
from rl_api.mail import Mailer, invitation_email, send_quietly
from rl_api.principal import OrgAccess, SessionUser, require_org, require_user, role_at_least
from rl_api.settings import Settings
from rl_api.validation import EmailStr, Name, Slug, not_found
from rl_core.plans import PlanLimits, limits_for
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin import accounts
from rl_db.admin.identity import create_organization
from rl_db.models import Invitation, Role
from rl_db.repositories import billing as billing_repo
from rl_db.repositories import orgs as orgs_repo
from rl_db.repositories.audit import AuditActor, record_audit

router = APIRouter(prefix="/api/v1", tags=["organizations"])

INVITE_TTL = timedelta(days=7)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]
UserDep = Annotated[SessionUser, Depends(require_user)]
Viewer = Annotated[OrgAccess, Depends(require_org(Role.VIEWER, api_key_scope="read"))]
ViewerSession = Annotated[OrgAccess, Depends(require_org(Role.VIEWER))]
Admin = Annotated[OrgAccess, Depends(require_org(Role.ADMIN))]
Owner = Annotated[OrgAccess, Depends(require_org(Role.OWNER))]


class PlanLimitsOut(BaseModel):
    plan: str
    name: str
    max_projects: int | None
    max_services: int | None
    retention_days: int
    slack_alerts: bool
    github_checks: bool
    price_usd_month: int | None
    self_serve: bool


def limits_out(limits: PlanLimits) -> PlanLimitsOut:
    return PlanLimitsOut(
        plan=limits.plan,
        name=limits.name,
        max_projects=limits.max_projects,
        max_services=limits.max_services,
        retention_days=limits.retention_days,
        slack_alerts=limits.slack_alerts,
        github_checks=limits.github_checks,
        price_usd_month=limits.price_usd_month,
        self_serve=limits.self_serve,
    )


class OrgCreate(BaseModel):
    name: Name
    slug: Slug


class OrgUpdate(BaseModel):
    name: Name


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    limits: PlanLimitsOut
    role: str | None  # the caller's role (None for API keys)
    created_at: datetime


class OrgSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    role: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    joined_at: datetime


class RoleUpdate(BaseModel):
    role: Role


class InviteCreate(BaseModel):
    email: EmailStr
    role: Role = Role.MEMBER


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    created_at: datetime


class AcceptInvite(BaseModel):
    token: str


class AcceptedInvite(BaseModel):
    org_id: uuid.UUID
    role: str


def _invite_out(row: Invitation) -> InviteOut:
    return InviteOut(
        id=row.id,
        email=row.email,
        role=Role(row.role).value,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


# ----------------------------------------------------------------------------- organizations
@router.post(
    "/orgs",
    status_code=201,
    response_model=OrgOut,
    tags=["organizations"],
    summary="Create an organization (the caller becomes its owner)",
    responses={409: {"description": "Slug already taken"}},
)
async def create_org(
    body: OrgCreate, request: Request, user: UserDep, sessions: SessionsDep
) -> OrgOut:
    try:
        async with sessions.begin() as db:
            org = await create_organization(db, name=body.name, slug=body.slug)
            ctx = TenantContext(
                tenant_id=org.id, principal=Principal.USER_SESSION, actor_id=user.user_id
            )
            await orgs_repo.add_member(db, ctx, user_id=user.user_id, role=Role.OWNER)
            await record_audit(
                db,
                ctx,
                action=audit_log.ORG_CREATED,
                actor=_user_actor(user),
                target_type="organization",
                target_id=str(org.id),
                request=audit_log.request_info(request),
                details={"name": org.name, "slug": org.slug},
            )
    except IntegrityError as exc:
        raise ApiError(409, "slug_taken", "this organization slug is already taken") from exc
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        plan=org.plan.value,
        limits=limits_out(limits_for(org.plan.value)),
        role=Role.OWNER.value,
        created_at=org.created_at,
    )


def _user_actor(user: SessionUser) -> AuditActor:
    return AuditActor(actor_type="user", user_id=user.user_id, email=user.email)


@router.get("/orgs", response_model=list[OrgSummary], summary="Organizations I belong to")
async def list_my_orgs(user: UserDep, sessions: SessionsDep) -> list[OrgSummary]:
    async with sessions() as db:
        rows = await accounts.list_user_memberships(db, user.user_id)
    return [
        OrgSummary(id=m.tenant_id, name=m.org_name, slug=m.org_slug, plan=m.plan, role=m.role.value)
        for m in rows
    ]


@router.get("/orgs/{org_id}", response_model=OrgOut, summary="One organization")
async def get_org(access: Viewer, sessions: SessionsDep) -> OrgOut:
    async with sessions() as db:
        org = await orgs_repo.get_organization(db, access.ctx)
    if org is None:
        raise not_found("organization")
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        plan=org.plan.value,
        limits=limits_out(limits_for(org.plan.value)),
        role=access.role.value if access.role else None,
        created_at=org.created_at,
    )


@router.patch("/orgs/{org_id}", response_model=OrgOut, summary="Rename (admin)")
async def update_org(
    body: OrgUpdate, request: Request, access: Admin, sessions: SessionsDep
) -> OrgOut:
    async with sessions.begin() as db:
        org = await orgs_repo.get_organization(db, access.ctx, for_update=True)
        if org is None:
            raise not_found("organization")
        before = org.name
        await orgs_repo.rename_organization(db, access.ctx, body.name)
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.SETTINGS_CHANGED,
            target_type="organization",
            target_id=org.id,
            details={"field": "name", "from": before, "to": body.name},
        )
        org.name = body.name
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        plan=org.plan.value,
        limits=limits_out(limits_for(org.plan.value)),
        role=access.role.value if access.role else None,
        created_at=org.created_at,
    )


@router.delete(
    "/orgs/{org_id}",
    status_code=204,
    summary="Delete the organization and all its data (owner)",
    responses={409: {"description": "A subscription is still active"}},
)
async def delete_org(request: Request, access: Owner, sessions: SessionsDep) -> Response:
    async with sessions.begin() as db:
        org = await orgs_repo.get_organization(db, access.ctx, for_update=True)
        if org is None:
            raise not_found("organization")
        sub = await billing_repo.get_subscription(db, access.ctx)
        if sub is not None and sub.status in ("active", "trialing", "past_due", "unpaid"):
            raise ApiError(
                409,
                "subscription_active",
                "cancel the subscription in the billing portal before deleting the organization",
            )
        # Written first: the audit log outlives the tenant (no foreign key).
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.ORG_DELETED,
            target_type="organization",
            target_id=org.id,
            details={"name": org.name, "slug": org.slug},
        )
        await orgs_repo.delete_organization(db, access.ctx)
    return Response(status_code=204)


# ----------------------------------------------------------------------------- members
@router.get(
    "/orgs/{org_id}/members", response_model=list[MemberOut], summary="Members (any member)"
)
async def list_members(access: ViewerSession, sessions: SessionsDep) -> list[MemberOut]:
    async with sessions() as db:
        rows = await orgs_repo.list_members(db, access.ctx)
    return [
        MemberOut(
            user_id=r.user_id,
            email=r.email,
            display_name=r.display_name,
            role=r.role.value,
            joined_at=r.joined_at,
        )
        for r in rows
    ]


def _require_owner_for(access: OrgAccess, *roles: Role) -> None:
    if Role.OWNER in roles and access.role != Role.OWNER:
        raise ApiError(403, "forbidden", "only an owner can grant, change or remove the owner role")


@router.patch(
    "/orgs/{org_id}/members/{user_id}",
    response_model=MemberOut,
    summary="Change a member's role (admin; owner role needs an owner)",
    responses={409: {"description": "Would leave the organization without an owner"}},
)
async def change_role(
    user_id: uuid.UUID,
    body: RoleUpdate,
    request: Request,
    access: Admin,
    sessions: SessionsDep,
) -> MemberOut:
    async with sessions.begin() as db:
        await orgs_repo.get_organization(db, access.ctx, for_update=True)  # serialise
        current = await orgs_repo.get_member_role(db, access.ctx, user_id)
        if current is None:
            raise not_found("member")
        _require_owner_for(access, current, body.role)
        if (
            current == Role.OWNER
            and body.role != Role.OWNER
            and await orgs_repo.count_owners(db, access.ctx) <= 1
        ):
            raise ApiError(409, "last_owner", "the last owner cannot be demoted")
        if current != body.role:
            await orgs_repo.set_member_role(db, access.ctx, user_id, body.role)
            await audit_log.audit(
                db,
                access,
                request,
                audit_log.MEMBER_ROLE_CHANGED,
                target_type="user",
                target_id=user_id,
                details={"from": current.value, "to": body.role.value},
            )
        members = await orgs_repo.list_members(db, access.ctx)
    member = next(m for m in members if m.user_id == user_id)
    return MemberOut(
        user_id=member.user_id,
        email=member.email,
        display_name=member.display_name,
        role=member.role.value,
        joined_at=member.joined_at,
    )


@router.delete(
    "/orgs/{org_id}/members/{user_id}",
    status_code=204,
    summary="Remove a member (admin), or leave (yourself)",
    responses={409: {"description": "Would leave the organization without an owner"}},
)
async def remove_member(
    user_id: uuid.UUID,
    request: Request,
    access: ViewerSession,
    sessions: SessionsDep,
) -> Response:
    is_self = access.user is not None and access.user.user_id == user_id
    if not is_self and (access.role is None or not role_at_least(access.role, Role.ADMIN)):
        raise ApiError(403, "forbidden", "this action requires the admin role")
    async with sessions.begin() as db:
        await orgs_repo.get_organization(db, access.ctx, for_update=True)
        current = await orgs_repo.get_member_role(db, access.ctx, user_id)
        if current is None:
            raise not_found("member")
        if not is_self:
            _require_owner_for(access, current)
        if current == Role.OWNER and await orgs_repo.count_owners(db, access.ctx) <= 1:
            raise ApiError(409, "last_owner", "the last owner cannot leave or be removed")
        await orgs_repo.remove_member(db, access.ctx, user_id)
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.MEMBER_REMOVED,
            target_type="user",
            target_id=user_id,
            details={"role": current.value, "self": is_self},
        )
    return Response(status_code=204)


# ----------------------------------------------------------------------------- invitations
@router.post(
    "/orgs/{org_id}/invites",
    status_code=201,
    response_model=InviteOut,
    summary="Invite someone by e-mail (admin; inviting an owner needs an owner)",
    responses={409: {"description": "Already a member"}},
)
async def create_invite(
    body: InviteCreate,
    request: Request,
    background: BackgroundTasks,
    access: Admin,
    settings: SettingsDep,
    sessions: SessionsDep,
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> InviteOut:
    _require_owner_for(access, body.role)
    token = tokens.new_token(tokens.INVITE)
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        org = await orgs_repo.get_organization(db, access.ctx, for_update=True)
        if org is None:
            raise not_found("organization")
        if await orgs_repo.is_member_email(db, access.ctx, body.email):
            raise ApiError(409, "already_member", "this person is already a member")
        row = await orgs_repo.create_invitation(
            db,
            access.ctx,
            email=body.email,
            role=body.role,
            token_hash=tokens.hash_token(token),
            invited_by=access.user.user_id if access.user else None,
            expires_at=now + INVITE_TTL,
            now=now,
        )
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.MEMBER_INVITED,
            target_type="invitation",
            target_id=row.id,
            details={"email": row.email, "role": body.role.value},
        )
        org_name = org.name
    inviter = access.user.email if access.user else "An administrator"
    background.add_task(
        send_quietly,
        mailer,
        invitation_email(
            row.email,
            org_name=org_name,
            inviter=inviter,
            role=body.role.value,
            link=settings.web_url(f"/invites/accept#token={token}"),
        ),
    )
    return _invite_out(row)


@router.get(
    "/orgs/{org_id}/invites", response_model=list[InviteOut], summary="Pending invitations (admin)"
)
async def list_invites(access: Admin, sessions: SessionsDep) -> list[InviteOut]:
    async with sessions() as db:
        rows = await orgs_repo.list_pending_invitations(db, access.ctx, now=datetime.now(UTC))
    return [_invite_out(r) for r in rows]


@router.delete(
    "/orgs/{org_id}/invites/{invite_id}", status_code=204, summary="Withdraw an invitation (admin)"
)
async def revoke_invite(
    invite_id: uuid.UUID, request: Request, access: Admin, sessions: SessionsDep
) -> Response:
    async with sessions.begin() as db:
        row = await orgs_repo.revoke_invitation(db, access.ctx, invite_id, now=datetime.now(UTC))
        if row is None:
            raise not_found("invitation")
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.INVITE_REVOKED,
            target_type="invitation",
            target_id=invite_id,
            details={"email": row.email},
        )
    return Response(status_code=204)


@router.post(
    "/invites/accept",
    response_model=AcceptedInvite,
    summary="Accept an invitation (signed in with the invited address)",
    responses={
        400: {"description": "Invalid, used, withdrawn or expired invitation"},
        403: {"description": "Signed in with a different e-mail address"},
        409: {"description": "Already a member"},
    },
)
async def accept_invite(
    body: AcceptInvite, request: Request, user: UserDep, sessions: SessionsDep
) -> AcceptedInvite:
    if not tokens.well_formed(body.token, tokens.INVITE):
        raise ApiError(400, "invalid_token", "the invitation is invalid or has expired")
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        invite = await accounts.find_invitation_by_token(
            db, tokens.hash_token(body.token), for_update=True
        )
        if (
            invite is None
            or invite.accepted_at is not None
            or invite.revoked_at is not None
            or invite.expires_at <= now
        ):
            raise ApiError(400, "invalid_token", "the invitation is invalid or has expired")
        if invite.email.lower() != user.email.lower():
            raise ApiError(
                403,
                "invite_email_mismatch",
                "sign in with the e-mail address the invitation was sent to",
            )
        ctx = TenantContext(
            tenant_id=invite.tenant_id, principal=Principal.USER_SESSION, actor_id=user.user_id
        )
        role = Role(invite.role)
        if await orgs_repo.get_member_role(db, ctx, user.user_id) is not None:
            raise ApiError(409, "already_member", "you are already a member")
        await orgs_repo.add_member(db, ctx, user_id=user.user_id, role=role)
        await orgs_repo.mark_invitation_accepted(db, ctx, invite, user_id=user.user_id, now=now)
        await record_audit(
            db,
            ctx,
            action=audit_log.MEMBER_JOINED,
            actor=_user_actor(user),
            target_type="user",
            target_id=str(user.user_id),
            request=audit_log.request_info(request),
            details={"role": role.value, "invitation_id": str(invite.id)},
        )
    return AcceptedInvite(org_id=invite.tenant_id, role=role.value)
