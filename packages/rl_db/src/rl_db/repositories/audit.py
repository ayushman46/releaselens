"""Audit log writes and reads (tenant-scoped; tenant-less sign-in events live in
:mod:`rl_db.admin.audit`). Rows are insert-only: the database refuses UPDATE and DELETE."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.audit import AuditLogEntry
from rl_db.repositories._tenancy import tenant_scoped

MAX_USER_AGENT_LEN = 512
MAX_TARGET_ID_LEN = 200


@dataclass(frozen=True, slots=True)
class AuditActor:
    actor_type: str  # user | api_key | system | anonymous
    user_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    email: str | None = None


SYSTEM_ACTOR = AuditActor(actor_type="system")


@dataclass(frozen=True, slots=True)
class AuditRequestInfo:
    ip: str | None = None
    user_agent: str | None = None


def audit_values(
    *,
    tenant_id: uuid.UUID | None,
    action: str,
    actor: AuditActor,
    target_type: str | None,
    target_id: str | None,
    request: AuditRequestInfo | None,
    details: Mapping[str, Any] | None,
) -> dict[str, Any]:
    info = request or AuditRequestInfo()
    return {
        "id": uuid7(),
        "tenant_id": tenant_id,
        "actor_type": actor.actor_type,
        "actor_user_id": actor.user_id,
        "actor_api_key_id": actor.api_key_id,
        "actor_email": actor.email,
        "action": action,
        "target_type": target_type,
        "target_id": target_id[:MAX_TARGET_ID_LEN] if target_id else None,
        "ip": info.ip,
        "user_agent": info.user_agent[:MAX_USER_AGENT_LEN] if info.user_agent else None,
        "details": dict(details or {}),
    }


@tenant_scoped
async def record_audit(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    action: str,
    actor: AuditActor,
    target_type: str | None = None,
    target_id: str | None = None,
    request: AuditRequestInfo | None = None,
    details: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """Append one entry for the caller's tenant, inside the caller's transaction (so the
    entry exists if and only if the change it describes was committed)."""
    values = audit_values(
        tenant_id=ctx.tenant_id,
        action=action,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        request=request,
        details=details,
    )
    await session.execute(insert(AuditLogEntry).values(**values))
    return uuid.UUID(str(values["id"]))


@dataclass(frozen=True, slots=True)
class AuditFilter:
    action: str | None = None
    actor_user_id: uuid.UUID | None = None
    target_type: str | None = None
    target_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditCursor:
    created_at: datetime
    id: uuid.UUID


@tenant_scoped
async def list_audit(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    filters: AuditFilter,
    limit: int,
    after: AuditCursor | None = None,
) -> list[AuditLogEntry]:
    """Newest first, keyset-paginated on ``(created_at, id)``."""
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be within 1..5000")
    stmt = select(AuditLogEntry).where(AuditLogEntry.tenant_id == ctx.tenant_id)
    if filters.action is not None:
        stmt = stmt.where(AuditLogEntry.action == filters.action)
    if filters.actor_user_id is not None:
        stmt = stmt.where(AuditLogEntry.actor_user_id == filters.actor_user_id)
    if filters.target_type is not None:
        stmt = stmt.where(AuditLogEntry.target_type == filters.target_type)
    if filters.target_id is not None:
        stmt = stmt.where(AuditLogEntry.target_id == filters.target_id)
    if filters.since is not None:
        stmt = stmt.where(AuditLogEntry.created_at >= filters.since)
    if filters.until is not None:
        stmt = stmt.where(AuditLogEntry.created_at < filters.until)
    if after is not None:
        stmt = stmt.where(
            tuple_(AuditLogEntry.created_at, AuditLogEntry.id) < tuple_(after.created_at, after.id)
        )
    stmt = stmt.order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())
