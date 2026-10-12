"""Organizations and API-key lookup (cross-tenant by necessity; see package docstring)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_db.models.identity import ApiKey, Organization


async def create_organization(
    session: AsyncSession, *, name: str, slug: str, is_demo: bool = False
) -> Organization:
    org = Organization(id=uuid7(), name=name, slug=slug, is_demo=is_demo)
    session.add(org)
    await session.flush()
    return org


async def get_organization_by_slug(session: AsyncSession, slug: str) -> Organization | None:
    stmt = select(Organization).where(Organization.slug == slug)
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class ApiKeyCredential:
    """What authentication needs from an ``api_keys`` row, and nothing else."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    secret_hash: str
    scopes: frozenset[str]
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


async def find_api_key_by_prefix(session: AsyncSession, prefix: str) -> ApiKeyCredential | None:
    """Look up a key by its public prefix. The caller must still verify the secret hash.

    This runs before any tenant is known: the key *is* how the tenant is learned.
    """
    stmt = select(
        ApiKey.id,
        ApiKey.tenant_id,
        ApiKey.secret_hash,
        ApiKey.scopes,
        ApiKey.expires_at,
        ApiKey.revoked_at,
        ApiKey.last_used_at,
    ).where(ApiKey.prefix == prefix)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return ApiKeyCredential(
        id=row.id,
        tenant_id=row.tenant_id,
        secret_hash=row.secret_hash,
        scopes=frozenset(row.scopes),
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


async def touch_api_key(
    session: AsyncSession, key_id: uuid.UUID, *, now: datetime, min_interval: timedelta
) -> bool:
    """Record usage at most once per ``min_interval`` per key. Returns True if it wrote.

    The condition lives in the UPDATE, so concurrent requests on several API processes write
    at most once per interval without coordinating. ``updated_at`` is pinned to its current
    value: it tracks changes to the key's configuration, not traffic.
    """
    stmt = (
        update(ApiKey)
        .where(
            ApiKey.id == key_id,
            or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at <= now - min_interval),
        )
        .values(last_used_at=now, updated_at=ApiKey.updated_at)
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))
