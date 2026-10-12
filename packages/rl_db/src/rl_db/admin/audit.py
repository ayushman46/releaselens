"""Tenant-less audit entries: sign-in success and failure (they belong to a user, or to no
known user at all, never to one organization)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_db.models.audit import AuditLogEntry
from rl_db.repositories.audit import AuditActor, AuditRequestInfo, audit_values


async def record_security_event(
    session: AsyncSession,
    *,
    action: str,
    actor: AuditActor,
    request: AuditRequestInfo | None = None,
    details: Mapping[str, Any] | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> uuid.UUID:
    values = audit_values(
        tenant_id=None,
        action=action,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        request=request,
        details=details,
    )
    await session.execute(insert(AuditLogEntry).values(**values))
    return uuid.UUID(str(values["id"]))
