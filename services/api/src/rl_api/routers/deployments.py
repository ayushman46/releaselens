"""Deployment events: intake (``POST``) and read-back (``GET``), spec §10.

Intake is asynchronous on purpose. ``POST`` validates the event, wraps it in a versioned
envelope and writes it to Kafka, then answers ``202`` with the ``event_id`` once the broker
has acknowledged it. The worker persists it shortly after. A client retrying after a
timeout or a 503 should resend the **same** ``event_id``: the worker deduplicates on
``(tenant_id, event_id)``, so retries never create a second deployment.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from rl_api.auth import require_scope
from rl_api.deps import get_producer, get_session
from rl_api.errors import ApiError
from rl_api.kafka import EventProducer, ProduceError
from rl_api.principal import require_tenant_read
from rl_api.ratelimit import limit_api_key
from rl_core.events import (
    DEPLOYMENT_EVENTS,
    V2_ONLY_FIELDS,
    DeploymentPayloadV1,
    DeploymentPayloadV2,
    deployment_key,
    encode_envelope,
    make_envelope,
)
from rl_core.events.deployment import Identifier
from rl_core.tenancy import TenantContext
from rl_db.models import AnalysisJob, Deployment
from rl_db.repositories import (
    DeploymentCursor,
    get_deployment,
    list_analysis_jobs,
    list_deployments,
)
from rl_db.repositories.billing import increment_usage, month_start

log = logging.getLogger("rl_api.deployments")

router = APIRouter(prefix="/api/v1/deployments", tags=["deployments"])

# deployed_at may run slightly ahead of our clock (CI runner skew). Further ahead than this
# is a client bug, and it would schedule the analysis jobs far in the future.
MAX_FUTURE_SKEW = timedelta(hours=1)

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "The API key lacks the required scope"},
}

# One callable, so FastAPI authenticates the key once per request (dependency caching).
_write_scope = require_scope("deployments:write")
# Ingest is rate limited per API key (token bucket in Postgres, RL_RATE_LIMIT_INGEST_*).
_ingest = limit_api_key(_write_scope, "ingest")


def _reject_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("schema_version must be an integer")
    return value


class DeploymentCreate(DeploymentPayloadV2):
    """Request body: the payload fields, plus an optional client-chosen ``event_id`` and
    ``schema_version`` (default 2). ``tenant_id`` is not accepted; it comes from the key."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: Identifier | None = None
    schema_version: Annotated[Literal[1, 2], BeforeValidator(_reject_bool)] = 2


class DeploymentAccepted(BaseModel):
    event_id: uuid.UUID


class AnalysisJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    status: str
    run_at: datetime
    attempts: int


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    schema_version: int
    service: str
    environment: str
    version: str
    previous_version: str | None
    commit_sha: str
    deployed_at: datetime
    deployer: str | None
    ci_run_url: str | None
    repository: str | None
    pull_request: int | None
    risk_assessment_id: uuid.UUID | None
    labels: dict[str, str]
    received_at: datetime


class DeploymentDetail(DeploymentOut):
    analysis_jobs: list[AnalysisJobOut]


class DeploymentPage(BaseModel):
    items: list[DeploymentOut]
    next_cursor: str | None


def _payload(body: DeploymentCreate) -> DeploymentPayloadV1:
    fields = body.model_dump(exclude={"event_id", "schema_version"})
    if body.schema_version == 1:
        sent_v2 = sorted(V2_ONLY_FIELDS & body.model_fields_set)
        if sent_v2:
            raise ApiError(
                422,
                "validation_error",
                f"schema_version 1 does not support: {', '.join(sent_v2)}",
            )
        return DeploymentPayloadV1.model_validate(
            {k: v for k, v in fields.items() if k not in V2_ONLY_FIELDS}
        )
    return DeploymentPayloadV2.model_validate(fields)


@router.post(
    "",
    status_code=202,
    response_model=DeploymentAccepted,
    summary="Record a completed deployment",
    responses={
        **_ERROR_RESPONSES,
        429: {"description": "Rate limit for this API key exceeded; see Retry-After"},
        503: {"description": "The event bus did not acknowledge the event; retry"},
    },
)
async def create_deployment(
    body: DeploymentCreate,
    request: Request,
    ctx: Annotated[TenantContext, Depends(_ingest)],
    producer: Annotated[EventProducer, Depends(get_producer)],
) -> DeploymentAccepted:
    if body.deployed_at > datetime.now(UTC) + MAX_FUTURE_SKEW:
        raise ApiError(422, "validation_error", "deployed_at is more than 1 hour in the future")
    payload = _payload(body)
    envelope = make_envelope(tenant_id=ctx.tenant_id, payload=payload, event_id=body.event_id)
    try:
        delivery = await producer.produce(
            DEPLOYMENT_EVENTS,
            key=deployment_key(ctx.tenant_id, payload.service),
            value=encode_envelope(envelope),
            headers=[
                ("content-type", b"application/json"),
                ("rl-event-type", envelope.type.encode()),
                ("rl-schema-version", str(envelope.schema_version).encode()),
            ],
        )
    except ProduceError as exc:
        log.warning(
            "deployment event not acknowledged by kafka",
            extra={"event_id": str(envelope.event_id), "error": str(exc)},
        )
        # The event_id is returned even on failure: a client that let us generate it can
        # still retry idempotently (if the event was in fact written, the worker dedupes).
        raise ApiError(
            503,
            "event_bus_unavailable",
            "the event could not be recorded; retry with the same event_id",
            details={"event_id": str(envelope.event_id)},
            headers={"Retry-After": "5"},
        ) from exc
    log.info(
        "deployment event accepted",
        extra={
            "event_id": str(envelope.event_id),
            "service": payload.service,
            "environment": payload.environment,
            "schema_version": envelope.schema_version,
            "partition": delivery.partition,
            "offset": delivery.offset,
        },
    )
    await _count_usage(request, ctx)
    return DeploymentAccepted(event_id=envelope.event_id)


async def _count_usage(request: Request, ctx: TenantContext) -> None:
    """Monthly ``deployments`` usage counter. Bookkeeping must never fail an accepted
    event, so errors are logged and swallowed."""
    try:
        async with request.app.state.db_sessions.begin() as session:
            await increment_usage(
                session, ctx, "deployments", period_start=month_start(datetime.now(UTC).date())
            )
    except Exception:
        log.warning("could not record deployment usage", exc_info=True)


def _encode_cursor(row: Deployment) -> str:
    raw = json.dumps({"t": row.deployed_at.isoformat(), "id": str(row.id)}).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> DeploymentCursor:
    """Decode a client-supplied cursor. It is untrusted input: any shape other than the one
    :func:`_encode_cursor` writes is a 400, never a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("cursor must be an object")
        t, row_id = data.get("t"), data.get("id")
        if not isinstance(t, str) or not isinstance(row_id, str):
            raise TypeError("cursor fields must be strings")
        deployed_at = datetime.fromisoformat(t)
        if deployed_at.tzinfo is None:
            raise ValueError("naive timestamp")
        # Normalise here: an offset that pushes the instant outside datetime's range (year 1
        # or 9999) raises OverflowError now instead of failing inside the database driver.
        deployed_at = deployed_at.astimezone(UTC)
        return DeploymentCursor(deployed_at=deployed_at, id=uuid.UUID(row_id))
    except (binascii.Error, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor is malformed") from exc


@router.get(
    "",
    response_model=DeploymentPage,
    summary="List deployments, newest first",
    responses=_ERROR_RESPONSES,
)
async def list_deployments_route(
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[str | None, Query(max_length=200)] = None,
    environment: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> DeploymentPage:
    after = _decode_cursor(cursor) if cursor else None
    rows = await list_deployments(
        session, ctx, limit=limit + 1, service=service, environment=environment, after=after
    )
    page, has_more = rows[:limit], len(rows) > limit
    return DeploymentPage(
        items=[DeploymentOut.model_validate(r) for r in page],
        next_cursor=_encode_cursor(page[-1]) if has_more else None,
    )


@router.get(
    "/{deployment_id}",
    response_model=DeploymentDetail,
    summary="One deployment with its analysis jobs",
    responses={**_ERROR_RESPONSES, 404: {"description": "No such deployment for this tenant"}},
)
async def get_deployment_route(
    deployment_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeploymentDetail:
    row = await get_deployment(session, ctx, deployment_id)
    if row is None:
        # Same answer for "does not exist" and "belongs to another tenant" (ADR-0010 §4).
        raise ApiError(404, "not_found", "deployment not found")
    jobs: list[AnalysisJob] = await list_analysis_jobs(session, ctx, row.id)
    out = DeploymentOut.model_validate(row).model_dump()
    return DeploymentDetail(**out, analysis_jobs=[AnalysisJobOut.model_validate(j) for j in jobs])
