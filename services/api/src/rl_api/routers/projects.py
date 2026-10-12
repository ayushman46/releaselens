"""Projects → services → environments (tenant-scoped CRUD).

Roles: any member (and a ``read`` API key) reads; **member** and above create and edit
projects, services and environments and delete services and environments; deleting a
whole project (which cascades to its services, environments and alert rules) needs
**admin**. Creating projects and services is checked against the plan's limits under a
lock on the organization row, so two concurrent creates cannot both squeeze past a limit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import audit as audit_log
from rl_api.deps import get_app_settings, get_sessions
from rl_api.errors import ApiError
from rl_api.principal import OrgAccess, require_org
from rl_api.settings import Settings
from rl_api.validation import Name, ProjectSlug, not_found, plan_limit_error
from rl_core.events.deployment import MAX_ENVIRONMENT_LEN, MAX_SERVICE_LEN
from rl_core.plans import allows_count, limits_for
from rl_db.models import Environment, Project, Role, Service
from rl_db.repositories import catalog
from rl_db.repositories import orgs as orgs_repo

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects", tags=["projects"])

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]
Reader = Annotated[OrgAccess, Depends(require_org(Role.VIEWER, api_key_scope="read"))]
Member = Annotated[OrgAccess, Depends(require_org(Role.MEMBER))]
Admin = Annotated[OrgAccess, Depends(require_org(Role.ADMIN))]

GitHubOwner = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")]
GitHubRepo = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
ServiceName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_SERVICE_LEN,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]
EnvName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=MAX_ENVIRONMENT_LEN, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    ),
]
Description = Annotated[
    str, StringConstraints(max_length=500, pattern=r"^[^\x00-\x08\x0b-\x1f\x7f]*$")
]


# ----------------------------------------------------------------------------- models
class GitHubRepoIn(BaseModel):
    owner: GitHubOwner
    repo: GitHubRepo
    installation_id: int | None = Field(default=None, ge=1)


class ProjectCreate(BaseModel):
    name: Name
    slug: ProjectSlug
    github: GitHubRepoIn | None = None


class ProjectUpdate(BaseModel):
    """Only the fields sent are changed; ``github: null`` disconnects the repository."""

    name: Name | None = None
    slug: ProjectSlug | None = None
    github: GitHubRepoIn | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> ProjectUpdate:
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        return self


class GitHubRepoOut(BaseModel):
    owner: str
    repo: str
    installation_id: int | None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    github: GitHubRepoOut | None
    created_at: datetime
    updated_at: datetime


class ServiceCreate(BaseModel):
    name: ServiceName
    description: Description | None = None


class ServiceUpdate(BaseModel):
    name: ServiceName | None = None
    description: Description | None = None


class ServiceOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class EnvironmentIn(BaseModel):
    name: EnvName


class EnvironmentOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    created_at: datetime


def project_out(p: Project) -> ProjectOut:
    github = (
        GitHubRepoOut(
            owner=p.github_owner, repo=p.github_repo, installation_id=p.github_installation_id
        )
        if p.github_owner and p.github_repo
        else None
    )
    return ProjectOut(
        id=p.id,
        name=p.name,
        slug=p.slug,
        github=github,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def service_out(s: Service) -> ServiceOut:
    return ServiceOut(
        id=s.id,
        project_id=s.project_id,
        name=s.name,
        description=s.description,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def environment_out(e: Environment) -> EnvironmentOut:
    return EnvironmentOut(id=e.id, project_id=e.project_id, name=e.name, created_at=e.created_at)


async def _plan(db: AsyncSession, access: OrgAccess) -> str:
    org = await orgs_repo.get_organization(db, access.ctx, for_update=True)
    if org is None:
        raise not_found("organization")
    return org.plan.value


# ----------------------------------------------------------------------------- projects
@router.get("", response_model=list[ProjectOut], summary="List projects")
async def list_projects(access: Reader, sessions: SessionsDep) -> list[ProjectOut]:
    async with sessions() as db:
        return [project_out(p) for p in await catalog.list_projects(db, access.ctx)]


@router.post(
    "",
    status_code=201,
    response_model=ProjectOut,
    summary="Create a project (member; plan limit)",
    responses={402: {"description": "Plan limit reached"}, 409: {"description": "Slug taken"}},
)
async def create_project(
    body: ProjectCreate,
    request: Request,
    access: Member,
    settings: SettingsDep,
    sessions: SessionsDep,
) -> ProjectOut:
    try:
        async with sessions.begin() as db:
            plan = await _plan(db, access)
            limits = limits_for(plan)
            current = await catalog.count_projects(db, access.ctx)
            if not allows_count(limits.max_projects, current):
                raise plan_limit_error(
                    settings,
                    limit="projects",
                    plan=plan,
                    maximum=limits.max_projects,
                    current=current,
                )
            row = await catalog.create_project(
                db,
                access.ctx,
                name=body.name,
                slug=body.slug,
                github_owner=body.github.owner if body.github else None,
                github_repo=body.github.repo if body.github else None,
                github_installation_id=body.github.installation_id if body.github else None,
                created_by=access.user.user_id if access.user else None,
            )
            await audit_log.audit(
                db,
                access,
                request,
                audit_log.PROJECT_CREATED,
                target_type="project",
                target_id=row.id,
                details={"name": row.name, "slug": row.slug},
            )
    except IntegrityError as exc:
        raise ApiError(409, "slug_taken", "a project with this slug already exists") from exc
    return project_out(row)


async def _project(db: AsyncSession, access: OrgAccess, project_id: uuid.UUID) -> Project:
    row = await catalog.get_project(db, access.ctx, project_id)
    if row is None:
        raise not_found("project")
    return row


@router.get("/{project_id}", response_model=ProjectOut, summary="One project")
async def get_project(project_id: uuid.UUID, access: Reader, sessions: SessionsDep) -> ProjectOut:
    async with sessions() as db:
        return project_out(await _project(db, access, project_id))


@router.patch(
    "/{project_id}",
    response_model=ProjectOut,
    summary="Update a project or its GitHub connection (member)",
    responses={409: {"description": "Slug taken"}},
)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    request: Request,
    access: Member,
    sessions: SessionsDep,
) -> ProjectOut:
    values: dict[str, Any] = {}
    if "name" in body.model_fields_set and body.name is not None:
        values["name"] = body.name
    if "slug" in body.model_fields_set and body.slug is not None:
        values["slug"] = body.slug
    if "github" in body.model_fields_set:
        gh = body.github
        values.update(
            github_owner=gh.owner if gh else None,
            github_repo=gh.repo if gh else None,
            github_installation_id=gh.installation_id if gh else None,
        )
    try:
        async with sessions.begin() as db:
            row = await catalog.update_project(db, access.ctx, project_id, values)
            if row is None:
                raise not_found("project")
            await audit_log.audit(
                db,
                access,
                request,
                audit_log.PROJECT_UPDATED,
                target_type="project",
                target_id=project_id,
                details={"fields": sorted(values)},
            )
    except IntegrityError as exc:
        raise ApiError(409, "slug_taken", "a project with this slug already exists") from exc
    return project_out(row)


@router.delete(
    "/{project_id}", status_code=204, summary="Delete a project and its children (admin)"
)
async def delete_project(
    project_id: uuid.UUID, request: Request, access: Admin, sessions: SessionsDep
) -> Response:
    async with sessions.begin() as db:
        row = await _project(db, access, project_id)
        await catalog.delete_project(db, access.ctx, project_id)
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.PROJECT_DELETED,
            target_type="project",
            target_id=project_id,
            details={"name": row.name, "slug": row.slug},
        )
    return Response(status_code=204)


# ----------------------------------------------------------------------------- services
@router.get("/{project_id}/services", response_model=list[ServiceOut], summary="List services")
async def list_services(
    project_id: uuid.UUID, access: Reader, sessions: SessionsDep
) -> list[ServiceOut]:
    async with sessions() as db:
        await _project(db, access, project_id)
        return [service_out(s) for s in await catalog.list_services(db, access.ctx, project_id)]


@router.post(
    "/{project_id}/services",
    status_code=201,
    response_model=ServiceOut,
    summary="Register a service (member; plan limit; names are unique per organization)",
    responses={402: {"description": "Plan limit reached"}, 409: {"description": "Name taken"}},
)
async def create_service(
    project_id: uuid.UUID,
    body: ServiceCreate,
    access: Member,
    settings: SettingsDep,
    sessions: SessionsDep,
) -> ServiceOut:
    try:
        async with sessions.begin() as db:
            plan = await _plan(db, access)
            await _project(db, access, project_id)
            limits = limits_for(plan)
            current = await catalog.count_services(db, access.ctx)
            if not allows_count(limits.max_services, current):
                raise plan_limit_error(
                    settings,
                    limit="services",
                    plan=plan,
                    maximum=limits.max_services,
                    current=current,
                )
            row = await catalog.create_service(
                db, access.ctx, project_id=project_id, name=body.name, description=body.description
            )
    except IntegrityError as exc:
        raise ApiError(409, "name_taken", "a service with this name already exists") from exc
    return service_out(row)


@router.get("/{project_id}/services/{service_id}", response_model=ServiceOut, summary="One service")
async def get_service(
    project_id: uuid.UUID, service_id: uuid.UUID, access: Reader, sessions: SessionsDep
) -> ServiceOut:
    async with sessions() as db:
        row = await catalog.get_service(db, access.ctx, project_id, service_id)
    if row is None:
        raise not_found("service")
    return service_out(row)


@router.patch(
    "/{project_id}/services/{service_id}",
    response_model=ServiceOut,
    summary="Rename or describe a service (member)",
    responses={409: {"description": "Name taken"}},
)
async def update_service(
    project_id: uuid.UUID,
    service_id: uuid.UUID,
    body: ServiceUpdate,
    access: Member,
    sessions: SessionsDep,
) -> ServiceOut:
    values = {k: getattr(body, k) for k in body.model_fields_set}
    if values.get("name") is None:
        values.pop("name", None)
    try:
        async with sessions.begin() as db:
            row = await catalog.update_service(db, access.ctx, project_id, service_id, values)
    except IntegrityError as exc:
        raise ApiError(409, "name_taken", "a service with this name already exists") from exc
    if row is None:
        raise not_found("service")
    return service_out(row)


@router.delete(
    "/{project_id}/services/{service_id}", status_code=204, summary="Delete a service (member)"
)
async def delete_service(
    project_id: uuid.UUID, service_id: uuid.UUID, access: Member, sessions: SessionsDep
) -> Response:
    async with sessions.begin() as db:
        if not await catalog.delete_service(db, access.ctx, project_id, service_id):
            raise not_found("service")
    return Response(status_code=204)


# ----------------------------------------------------------------------------- environments
@router.get(
    "/{project_id}/environments", response_model=list[EnvironmentOut], summary="List environments"
)
async def list_environments(
    project_id: uuid.UUID, access: Reader, sessions: SessionsDep
) -> list[EnvironmentOut]:
    async with sessions() as db:
        await _project(db, access, project_id)
        return [
            environment_out(e) for e in await catalog.list_environments(db, access.ctx, project_id)
        ]


@router.post(
    "/{project_id}/environments",
    status_code=201,
    response_model=EnvironmentOut,
    summary="Add an environment (member)",
    responses={409: {"description": "Name taken"}},
)
async def create_environment(
    project_id: uuid.UUID, body: EnvironmentIn, access: Member, sessions: SessionsDep
) -> EnvironmentOut:
    try:
        async with sessions.begin() as db:
            await _project(db, access, project_id)
            row = await catalog.create_environment(
                db, access.ctx, project_id=project_id, name=body.name
            )
    except IntegrityError as exc:
        raise ApiError(409, "name_taken", "this environment already exists") from exc
    return environment_out(row)


@router.patch(
    "/{project_id}/environments/{environment_id}",
    response_model=EnvironmentOut,
    summary="Rename an environment (member)",
    responses={409: {"description": "Name taken"}},
)
async def rename_environment(
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    body: EnvironmentIn,
    access: Member,
    sessions: SessionsDep,
) -> EnvironmentOut:
    try:
        async with sessions.begin() as db:
            row = await catalog.rename_environment(
                db, access.ctx, project_id, environment_id, body.name
            )
    except IntegrityError as exc:
        raise ApiError(409, "name_taken", "this environment already exists") from exc
    if row is None:
        raise not_found("environment")
    return environment_out(row)


@router.delete(
    "/{project_id}/environments/{environment_id}",
    status_code=204,
    summary="Delete an environment (member)",
)
async def delete_environment(
    project_id: uuid.UUID, environment_id: uuid.UUID, access: Member, sessions: SessionsDep
) -> Response:
    async with sessions.begin() as db:
        if not await catalog.delete_environment(db, access.ctx, project_id, environment_id):
            raise not_found("environment")
    return Response(status_code=204)
