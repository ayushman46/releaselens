"""Audit log: filtered, keyset-paginated listing and CSV export (owner and admin).

CSV export is safe to open in a spreadsheet: every cell is quoted (RFC 4180 escaping via
:mod:`csv`) and any cell starting with ``=``, ``+``, ``-``, ``@``, tab or carriage return
is prefixed with ``'`` so it is shown as text instead of being evaluated as a formula
(OWASP "CSV injection").
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.deps import get_sessions
from rl_api.errors import ApiError
from rl_api.principal import OrgAccess, require_org
from rl_db.models import AuditLogEntry, Role
from rl_db.repositories.audit import AuditCursor, AuditFilter, list_audit

router = APIRouter(prefix="/api/v1/orgs/{org_id}/audit-log", tags=["audit-log"])

Admin = Annotated[OrgAccess, Depends(require_org(Role.ADMIN))]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]

MAX_EXPORT_ROWS = 50_000
_EXPORT_PAGE = 1000
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
CSV_COLUMNS = (
    "created_at",
    "action",
    "actor_type",
    "actor_user_id",
    "actor_email",
    "actor_api_key_id",
    "target_type",
    "target_id",
    "ip",
    "user_agent",
    "metadata",
    "id",
)


class AuditEntryOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    action: str
    actor_type: str
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    actor_api_key_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    ip: str | None
    user_agent: str | None
    metadata: dict[str, Any]


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None


def entry_out(e: AuditLogEntry) -> AuditEntryOut:
    return AuditEntryOut(
        id=e.id,
        created_at=e.created_at,
        action=e.action,
        actor_type=e.actor_type,
        actor_user_id=e.actor_user_id,
        actor_email=e.actor_email,
        actor_api_key_id=e.actor_api_key_id,
        target_type=e.target_type,
        target_id=e.target_id,
        ip=str(e.ip) if e.ip is not None else None,
        user_agent=e.user_agent,
        metadata=dict(e.details),
    )


def _encode_cursor(e: AuditLogEntry) -> str:
    raw = json.dumps({"t": e.created_at.isoformat(), "id": str(e.id)}).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> AuditCursor:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(data, dict):
            raise TypeError("cursor must be an object")
        created = datetime.fromisoformat(str(data["t"]))
        if created.tzinfo is None:
            raise ValueError("naive timestamp")
        return AuditCursor(created_at=created.astimezone(UTC), id=uuid.UUID(str(data["id"])))
    except (binascii.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor is malformed") from exc


def _filters(
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor_user_id: uuid.UUID | None = None,
    target_type: Annotated[str | None, Query(max_length=64)] = None,
    target_id: Annotated[str | None, Query(max_length=200)] = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> AuditFilter:
    return AuditFilter(
        action=action,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        since=since,
        until=until,
    )


FiltersDep = Annotated[AuditFilter, Depends(_filters)]


@router.get("", response_model=AuditPage, summary="Audit log, newest first (owner/admin)")
async def list_entries(
    access: Admin,
    sessions: SessionsDep,
    filters: FiltersDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> AuditPage:
    after = _decode_cursor(cursor) if cursor else None
    async with sessions() as db:
        rows = await list_audit(db, access.ctx, filters=filters, limit=limit + 1, after=after)
    page, more = rows[:limit], len(rows) > limit
    return AuditPage(
        items=[entry_out(e) for e in page],
        next_cursor=_encode_cursor(page[-1]) if more else None,
    )


def safe_cell(value: object) -> str:
    """One CSV cell, neutralised against spreadsheet formula injection."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def csv_row(e: AuditEntryOut) -> list[str]:
    data = e.model_dump(mode="json")
    data["metadata"] = json.dumps(e.metadata, sort_keys=True, separators=(",", ":"))
    return [safe_cell(data[c]) for c in CSV_COLUMNS]


@router.get(
    "/export.csv",
    summary="Export the filtered audit log as CSV (owner/admin; at most 50,000 rows)",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV file"}},
)
async def export_csv(
    access: Admin, sessions: SessionsDep, filters: FiltersDep
) -> StreamingResponse:
    async def rows() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        yield buffer.getvalue()
        after: AuditCursor | None = None
        sent = 0
        async with sessions() as db:
            while sent < MAX_EXPORT_ROWS:
                batch = await list_audit(
                    db,
                    access.ctx,
                    filters=filters,
                    limit=min(_EXPORT_PAGE, MAX_EXPORT_ROWS - sent),
                    after=after,
                )
                if not batch:
                    break
                buffer.seek(0)
                buffer.truncate()
                for e in batch:
                    writer.writerow(csv_row(entry_out(e)))
                yield buffer.getvalue()
                sent += len(batch)
                after = AuditCursor(created_at=batch[-1].created_at, id=batch[-1].id)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="audit-log-{access.org_id}-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )
