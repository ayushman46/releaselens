"""API key rows (tenant-scoped). Key generation and hashing live in ``rl_api.apikeys``."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.identity import ApiKey
from rl_db.repositories._tenancy import tenant_scoped


@tenant_scoped
async def insert_api_key(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    name: str,
    prefix: str,
    secret_hash: str,
    scopes: Iterable[str],
    expires_at: datetime | None = None,
    created_by: uuid.UUID | None = None,
) -> ApiKey:
    """Store a key's *hash*; the plaintext never reaches this layer."""
    key = ApiKey(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        name=name,
        prefix=prefix,
        secret_hash=secret_hash,
        scopes=sorted(set(scopes)),
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(key)
    await session.flush()
    return key


@tenant_scoped
async def list_api_keys(session: AsyncSession, ctx: TenantContext) -> list[ApiKey]:
    stmt = (
        select(ApiKey)
        .where(ApiKey.tenant_id == ctx.tenant_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
    )
    return list((await session.execute(stmt)).scalars())


@tenant_scoped
async def get_api_key(
    session: AsyncSession, ctx: TenantContext, key_id: uuid.UUID, *, for_update: bool = False
) -> ApiKey | None:
    """One of the tenant's keys, or None (another tenant's id looks the same as no key)."""
    stmt = select(ApiKey).where(ApiKey.tenant_id == ctx.tenant_id, ApiKey.id == key_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


@tenant_scoped
async def revoke_api_key(
    session: AsyncSession, ctx: TenantContext, key_id: uuid.UUID, *, at: datetime | None = None
) -> bool:
    """Revoke one of the tenant's keys. False if it does not exist (for this tenant) or is
    already revoked. Revocation is immediate: authentication reads ``revoked_at`` on every
    request, never caches a positive result, and treats any non-null ``revoked_at`` as
    revoked. ``at`` is only the time recorded for audit; it cannot schedule a revocation
    (use ``expires_at`` for that)."""
    stmt = (
        update(ApiKey)
        .where(ApiKey.tenant_id == ctx.tenant_id, ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=at or datetime.now(UTC))
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))
