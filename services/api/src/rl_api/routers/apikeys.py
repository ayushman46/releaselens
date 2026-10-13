"""API key management (admin and owner only, signed-in users only; ADR-0016).

The full key is returned **once**, in the create and rotate responses, and never again:
only its SHA-256 is stored. Rotation creates a replacement with the same name, scopes and
expiry and revokes the old key in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import audit as audit_log
from rl_api.apikeys import KNOWN_SCOPES, MAX_KEY_NAME_LEN, create_api_key, normalize_scopes
from rl_api.deps import get_sessions
from rl_api.errors import ApiError
from rl_api.principal import OrgAccess, require_org
from rl_api.validation import not_found
from rl_db.models import ApiKey, Role
from rl_db.repositories import get_api_key, list_api_keys, revoke_api_key

router = APIRouter(prefix="/api/v1/orgs/{org_id}/api-keys", tags=["api-keys"])

Admin = Annotated[OrgAccess, Depends(require_org(Role.ADMIN))]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_KEY_NAME_LEN)
    scopes: list[str] = Field(min_length=1, max_length=len(KNOWN_SCOPES))
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        return sorted(normalize_scopes(value))

    @field_validator("expires_at")
    @classmethod
    def _future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        if value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_by: uuid.UUID | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full key. Shown only in this response; store it now.")


def key_out(row: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=sorted(row.scopes),
        created_by=row.created_by,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


@router.get("", response_model=list[ApiKeyOut], summary="List keys (admin)")
async def list_keys(access: Admin, sessions: SessionsDep) -> list[ApiKeyOut]:
    async with sessions() as db:
        return [key_out(k) for k in await list_api_keys(db, access.ctx)]


@router.post(
    "",
    status_code=201,
    response_model=ApiKeyCreated,
    summary="Create a key (admin); the secret is returned once",
)
async def create_key(
    body: ApiKeyCreate,
    request: Request,
    access: Admin,
    sessions: SessionsDep,
) -> ApiKeyCreated:
    async with sessions.begin() as db:
        created = await create_api_key(
            db,
            access.org_id,
            body.name,
            body.scopes,
            expires_at=body.expires_at,
            created_by=access.user.user_id if access.user else None,
        )
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.KEY_CREATED,
            target_type="api_key",
            target_id=created.id,
            details={
                "prefix": created.prefix,
                "name": created.name,
                "scopes": sorted(created.scopes),
            },
        )
        row = await get_api_key(db, access.ctx, created.id)
    assert row is not None
    return ApiKeyCreated(**key_out(row).model_dump(), key=created.plaintext)


@router.delete("/{key_id}", status_code=204, summary="Revoke a key (admin; immediate, idempotent)")
async def revoke_key(
    key_id: uuid.UUID,
    request: Request,
    access: Admin,
    sessions: SessionsDep,
) -> Response:
    async with sessions.begin() as db:
        row = await get_api_key(db, access.ctx, key_id, for_update=True)
        if row is None:
            raise not_found("API key")
        if await revoke_api_key(db, access.ctx, key_id):
            await audit_log.audit(
                db,
                access,
                request,
                audit_log.KEY_REVOKED,
                target_type="api_key",
                target_id=key_id,
                details={"prefix": row.prefix, "name": row.name},
            )
    return Response(status_code=204)


@router.post(
    "/{key_id}/rotate",
    status_code=201,
    response_model=ApiKeyCreated,
    summary="Replace a key (admin): new secret returned once, old key revoked",
    responses={409: {"description": "The key is already revoked"}},
)
async def rotate_key(
    key_id: uuid.UUID,
    request: Request,
    access: Admin,
    sessions: SessionsDep,
) -> ApiKeyCreated:
    async with sessions.begin() as db:
        old = await get_api_key(db, access.ctx, key_id, for_update=True)
        if old is None:
            raise not_found("API key")
        if old.revoked_at is not None:
            raise ApiError(409, "key_revoked", "a revoked key cannot be rotated; create a new one")
        expires_at = (
            old.expires_at if old.expires_at and old.expires_at > datetime.now(UTC) else None
        )
        created = await create_api_key(
            db,
            access.org_id,
            old.name,
            old.scopes,
            expires_at=expires_at,
            created_by=access.user.user_id if access.user else None,
        )
        await revoke_api_key(db, access.ctx, key_id)
        await audit_log.audit(
            db,
            access,
            request,
            audit_log.KEY_ROTATED,
            target_type="api_key",
            target_id=created.id,
            details={"old_key_id": str(key_id), "old_prefix": old.prefix, "prefix": created.prefix},
        )
        row = await get_api_key(db, access.ctx, created.id)
    assert row is not None
    return ApiKeyCreated(**key_out(row).model_dump(), key=created.plaintext)
