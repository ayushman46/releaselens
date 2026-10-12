"""Projects, services and environments (tenant-scoped, ADR-0010).

Lookups by id always filter on the caller's ``tenant_id`` *and* the parent id, so another
tenant's id, or a service id paired with the wrong project, looks exactly like a missing
row (the API answers 404 either way).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.catalog import Environment, Project, Service

# ----------------------------------------------------------------------------- projects


async def count_projects(session: AsyncSession, ctx: TenantContext) -> int:
    stmt = select(func.count()).where(Project.tenant_id == ctx.tenant_id)
    return int((await session.execute(stmt)).scalar_one())


async def create_project(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    name: str,
    slug: str,
    github_owner: str | None = None,
    github_repo: str | None = None,
    github_installation_id: int | None = None,
    created_by: uuid.UUID | None = None,
) -> Project:
    row = Project(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        name=name,
        slug=slug,
        github_owner=github_owner,
        github_repo=github_repo,
        github_installation_id=github_installation_id,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def list_projects(session: AsyncSession, ctx: TenantContext) -> list[Project]:
    stmt = (
        select(Project).where(Project.tenant_id == ctx.tenant_id).order_by(Project.name, Project.id)
    )
    return list((await session.execute(stmt)).scalars())


async def get_project(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID
) -> Project | None:
    stmt = select(Project).where(Project.tenant_id == ctx.tenant_id, Project.id == project_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_project(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, values: dict[str, Any]
) -> Project | None:
    if not values:
        return await get_project(session, ctx, project_id)
    stmt = (
        update(Project)
        .where(Project.tenant_id == ctx.tenant_id, Project.id == project_id)
        .values(**values, updated_at=func.now())
        .returning(Project)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_project(session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(Project).where(Project.tenant_id == ctx.tenant_id, Project.id == project_id)
    )
    return bool(getattr(result, "rowcount", 0))


# ----------------------------------------------------------------------------- services


async def count_services(session: AsyncSession, ctx: TenantContext) -> int:
    stmt = select(func.count()).where(Service.tenant_id == ctx.tenant_id)
    return int((await session.execute(stmt)).scalar_one())


async def create_service(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    project_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> Service:
    row = Service(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        name=name,
        description=description,
    )
    session.add(row)
    await session.flush()
    return row


async def list_services(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID
) -> list[Service]:
    stmt = (
        select(Service)
        .where(Service.tenant_id == ctx.tenant_id, Service.project_id == project_id)
        .order_by(Service.name, Service.id)
    )
    return list((await session.execute(stmt)).scalars())


async def get_service(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, service_id: uuid.UUID
) -> Service | None:
    stmt = select(Service).where(
        Service.tenant_id == ctx.tenant_id,
        Service.project_id == project_id,
        Service.id == service_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_service_by_name(
    session: AsyncSession, ctx: TenantContext, name: str
) -> Service | None:
    """The service a deployment event or span names (names are unique per tenant)."""
    stmt = select(Service).where(Service.tenant_id == ctx.tenant_id, Service.name == name)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_service(
    session: AsyncSession,
    ctx: TenantContext,
    project_id: uuid.UUID,
    service_id: uuid.UUID,
    values: dict[str, Any],
) -> Service | None:
    if not values:
        return await get_service(session, ctx, project_id, service_id)
    stmt = (
        update(Service)
        .where(
            Service.tenant_id == ctx.tenant_id,
            Service.project_id == project_id,
            Service.id == service_id,
        )
        .values(**values, updated_at=func.now())
        .returning(Service)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_service(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, service_id: uuid.UUID
) -> bool:
    result = await session.execute(
        delete(Service).where(
            Service.tenant_id == ctx.tenant_id,
            Service.project_id == project_id,
            Service.id == service_id,
        )
    )
    return bool(getattr(result, "rowcount", 0))


# ----------------------------------------------------------------------------- environments


async def create_environment(
    session: AsyncSession, ctx: TenantContext, *, project_id: uuid.UUID, name: str
) -> Environment:
    row = Environment(id=uuid7(), tenant_id=ctx.tenant_id, project_id=project_id, name=name)
    session.add(row)
    await session.flush()
    return row


async def list_environments(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID
) -> list[Environment]:
    stmt = (
        select(Environment)
        .where(Environment.tenant_id == ctx.tenant_id, Environment.project_id == project_id)
        .order_by(Environment.name, Environment.id)
    )
    return list((await session.execute(stmt)).scalars())


async def get_environment(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, environment_id: uuid.UUID
) -> Environment | None:
    stmt = select(Environment).where(
        Environment.tenant_id == ctx.tenant_id,
        Environment.project_id == project_id,
        Environment.id == environment_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def rename_environment(
    session: AsyncSession,
    ctx: TenantContext,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    name: str,
) -> Environment | None:
    stmt = (
        update(Environment)
        .where(
            Environment.tenant_id == ctx.tenant_id,
            Environment.project_id == project_id,
            Environment.id == environment_id,
        )
        .values(name=name, updated_at=func.now())
        .returning(Environment)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def delete_environment(
    session: AsyncSession, ctx: TenantContext, project_id: uuid.UUID, environment_id: uuid.UUID
) -> bool:
    result = await session.execute(
        delete(Environment).where(
            Environment.tenant_id == ctx.tenant_id,
            Environment.project_id == project_id,
            Environment.id == environment_id,
        )
    )
    return bool(getattr(result, "rowcount", 0))
