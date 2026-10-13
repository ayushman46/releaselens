"""GitHub App webhooks before a tenant is known (cross-tenant by necessity, ADR-0010).

* :func:`record_delivery`: deduplicate on ``X-GitHub-Delivery`` (every delivery, any tenant).
* :func:`find_installation`: installation id → tenant, which is how a delivery *finds out* its
  tenant. Everything after that runs in the tenant's own context.
* :func:`link_installation` / :func:`set_installation_suspended`: operator actions (admin CLI)
  and installation lifecycle events.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_db.models.github import MAX_GITHUB_LOGIN_LEN, GithubInstallation, WebhookDelivery


class InstallationLinkedElsewhereError(ValueError):
    """The installation already belongs to another organization."""


async def record_delivery(
    session: AsyncSession, *, delivery_id: str, event: str, installation_id: int | None
) -> bool:
    """Insert the delivery id. False if it was already recorded (a redelivery)."""
    stmt = (
        pg_insert(WebhookDelivery)
        .values(delivery_id=delivery_id, event=event, installation_id=installation_id)
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        .returning(WebhookDelivery.delivery_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def set_delivery_outcome(
    session: AsyncSession, delivery_id: str, *, outcome: str, tenant_id: uuid.UUID | None
) -> None:
    await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.delivery_id == delivery_id)
        .values(outcome=outcome[:64], tenant_id=tenant_id)
    )


async def delete_old_deliveries(session: AsyncSession, *, older_than: timedelta) -> int:
    """Retention: GitHub redelivers for days at most, so old ids are no longer needed."""
    cutoff = datetime.now(UTC) - older_than
    result = await session.execute(
        delete(WebhookDelivery).where(WebhookDelivery.received_at < cutoff)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def find_installation(
    session: AsyncSession, installation_id: int
) -> GithubInstallation | None:
    stmt = select(GithubInstallation).where(GithubInstallation.installation_id == installation_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def link_installation(
    session: AsyncSession, *, installation_id: int, tenant_id: uuid.UUID, account_login: str
) -> GithubInstallation:
    """Link an App installation to an organization (idempotent for the same organization)."""
    if installation_id <= 0:
        raise ValueError("installation_id must be positive")
    login = account_login.strip().lower()
    if not login or len(login) > MAX_GITHUB_LOGIN_LEN:
        raise ValueError("account_login must be a GitHub login (1-39 characters)")
    existing = await find_installation(session, installation_id)
    if existing is not None:
        if existing.tenant_id != tenant_id:
            raise InstallationLinkedElsewhereError(
                f"installation {installation_id} is linked to another organization"
            )
        existing.account_login = login
        existing.suspended_at = None
        await session.flush()
        return existing
    row = GithubInstallation(
        installation_id=installation_id, tenant_id=tenant_id, account_login=login
    )
    session.add(row)
    await session.flush()
    return row


async def set_installation_suspended(
    session: AsyncSession, installation_id: int, *, suspended_at: datetime | None
) -> bool:
    result = await session.execute(
        update(GithubInstallation)
        .where(GithubInstallation.installation_id == installation_id)
        .values(suspended_at=suspended_at)
    )
    return bool(getattr(result, "rowcount", 0))
