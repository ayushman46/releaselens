"""CI-failure risk assessments (spec §9; contract: docs/api/RISK_ASSESSMENTS.md).

``POST /api/v1/risk/assessments`` (scope ``risk:write``, rate limited per key) turns the change
facts the GitHub Action computed in the customer's runner, plus the tenant's own finished CI
runs of that repository, into a feature vector (``rl_core.features.online.compute_online``, the
same code the training mart used), scores it with the model loaded at startup, stores the
assessment and answers ``201``. The same ``(repository, commit_sha, event, pull_request)`` within
``RL_RISK_IDEMPOTENCY_WINDOW_S`` (10 minutes) answers ``200`` with the stored assessment.

A repository with no finished runs is scored by the no-history model (``cold_start: true``).
Without the needed model the answer is ``503 no_model_loaded``: never a made-up score.

Reads (``GET``) take an API key with ``read`` or a session plus ``X-RL-Org``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    field_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from rl_api.auth import require_scope
from rl_api.deps import get_session
from rl_api.errors import ApiError
from rl_api.github_app import GitHubAppClient, check_run_payload
from rl_api.principal import require_tenant_read
from rl_api.ratelimit import limit_api_key
from rl_api.risk import service
from rl_api.risk.modelstore import ModelStore
from rl_api.settings import Settings
from rl_core.events.deployment import HttpUrl, PullRequestNumber, Repository
from rl_core.features.change import BUILD_INPUT_FIELDS
from rl_core.features.online import compute_online
from rl_core.tenancy import TenantContext
from rl_db.repositories.alerts import band_meets, notify_risk
from rl_db.repositories.github import installation_for_repository
from rl_db.repositories.risk import (
    AssessmentCursor,
    find_recent_assessment,
    get_assessment,
    idempotency_key,
    insert_assessment,
    list_assessments,
    load_history,
    lock_idempotency_key,
)

log = logging.getLogger("rl_api.risk")

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])

MAX_FUTURE_SKEW = timedelta(hours=1)
_write = limit_api_key(require_scope("risk:write"), "risk")

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "The API key lacks the required scope"},
}

CommitSha = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$"),
]
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _no_control(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("must not contain control characters")
    return value


Branch = Annotated[
    str, StringConstraints(min_length=1, max_length=255), AfterValidator(_no_control)
]
FactValue = StrictBool | StrictInt | StrictFloat | None


class Extractor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[!-~]+$")]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[!-~]+$")]


class RiskAssessmentCreate(BaseModel):
    """Request body. ``tenant_id`` is not accepted: it comes from the API key."""

    model_config = ConfigDict(extra="forbid")

    repository: Repository
    commit_sha: CommitSha
    event: Literal["push", "pull_request"]
    triggered_at: AwareDatetime
    pull_request: PullRequestNumber | None = None
    branch: Branch | None = None
    ci_run_url: HttpUrl | None = None
    previous_built_sha: CommitSha | None = None
    extractor: Extractor | None = None
    change_facts: dict[str, FactValue] = Field(
        description="Keys from rl_core BUILD_INPUT_FIELDS; a missing key means unknown, never 0."
    )

    @field_validator("change_facts")
    @classmethod
    def _known_facts(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value) - set(BUILD_INPUT_FIELDS))
        if unknown:
            raise ValueError(f"unknown change facts: {', '.join(unknown)}")
        bad = sorted(k for k, v in value.items() if isinstance(v, float) and not math.isfinite(v))
        if bad:
            raise ValueError(f"change facts must be finite numbers: {', '.join(bad)}")
        return value


class Signal(BaseModel):
    feature: str
    features: list[str] = Field(default_factory=list)
    label: str
    value: float | None
    contribution: float
    text: str


class HistoryInfo(BaseModel):
    finished_builds_used: int
    cold_start: bool


class RiskAssessmentOut(BaseModel):
    assessment_id: uuid.UUID
    model_version: str
    feature_schema_version: str
    risk_score: int = Field(ge=0, le=100)
    risk_band: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    probability: float | None
    top_increasing: list[Signal]
    top_reducing: list[Signal]
    history: HistoryInfo
    details_url: str
    explanation_note: str | None = None
    repository: str
    commit_sha: str
    event: str
    pull_request: int | None
    triggered_at: datetime
    created_at: datetime


class ShapVector(BaseModel):
    unit: str
    base_value: float | None
    values: dict[str, float] | None


class RiskAssessmentDetail(RiskAssessmentOut):
    model_name: str
    raw_score: float
    branch: str | None
    ci_run_url: str | None
    previous_built_sha: str | None
    extractor: dict[str, Any] | None
    features: dict[str, float | None]
    change_facts: dict[str, Any]
    shap: ShapVector


class RiskAssessmentPage(BaseModel):
    items: list[RiskAssessmentOut]
    next_cursor: str | None


def _store(request: Request) -> ModelStore:
    store: ModelStore = request.app.state.model_store
    return store


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _no_model(role: str) -> ApiError:
    what = "the cold-start (no-history) model" if role == "cold" else "the champion model"
    return ApiError(
        503,
        "no_model_loaded",
        f"no risk model is loaded ({what}); no score was produced",
        details={"model": role},
        headers={"Retry-After": "60"},
    )


@router.post(
    "/assessments",
    status_code=201,
    response_model=RiskAssessmentOut,
    summary="Assess the CI-failure risk of a change",
    responses={
        **_ERRORS,
        200: {"description": "Idempotent replay: the assessment stored within the window"},
        422: {"description": "Invalid body, e.g. unknown change_facts keys"},
        429: {"description": "Rate limit for this API key exceeded; see Retry-After"},
        503: {"description": "No model loaded (no_model_loaded); never a fabricated score"},
    },
)
async def create_assessment(
    body: RiskAssessmentCreate,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    ctx: Annotated[TenantContext, Depends(_write)],
) -> RiskAssessmentOut:
    settings = _settings(request)
    store = _store(request)
    now = datetime.now(UTC)
    if body.triggered_at > now + MAX_FUTURE_SKEW:
        raise ApiError(422, "validation_error", "triggered_at is more than 1 hour in the future")
    repository = body.repository.lower()
    sha = body.commit_sha.lower()
    triggered_at = body.triggered_at.astimezone(UTC)
    facts: dict[str, Any] = dict(body.change_facts)
    check_run: tuple[int, dict[str, Any]] | None = None

    async with request.app.state.db_sessions.begin() as session:
        await lock_idempotency_key(
            session, idempotency_key(ctx, repository, sha, body.event, body.pull_request)
        )
        existing = await find_recent_assessment(
            session,
            ctx,
            repository=repository,
            commit_sha=sha,
            event=body.event,
            pull_request=body.pull_request,
            since=now - timedelta(seconds=settings.risk_idempotency_window_s),
        )
        if existing is not None:
            response.status_code = 200
            return RiskAssessmentOut.model_validate(
                service.assessment_summary(existing, settings.web_base_url)
            )
        if store.main is None:
            raise _no_model("main")
        history, totals = await load_history(
            session, ctx, repository, triggered_at, limit=settings.risk_history_limit
        )
        cold_start = totals.eligible == 0
        model = store.cold if cold_start else store.main
        if model is None:
            raise _no_model("cold")
        features = compute_online(facts, history, triggered_at, totals=totals)
        result = await asyncio.to_thread(service.score, model, features)
        row = await insert_assessment(
            session,
            ctx,
            {
                "repository": repository,
                "commit_sha": sha,
                "event": body.event,
                "pull_request": body.pull_request,
                "branch": body.branch,
                "ci_run_url": body.ci_run_url,
                "previous_built_sha": body.previous_built_sha.lower()
                if body.previous_built_sha
                else None,
                "extractor": body.extractor.model_dump() if body.extractor else None,
                "triggered_at": triggered_at,
                **service.row_values(
                    model=model,
                    result=result,
                    features=features,
                    change_facts=facts,
                    history_used=totals.eligible,
                    cold_start=cold_start,
                ),
            },
        )
        summary = service.assessment_summary(row, settings.web_base_url)
        if band_meets(row.risk_band, "HIGH"):
            await notify_risk(
                session,
                ctx,
                risk_assessment_id=row.id,
                band=row.risk_band,
                repository=repository,
                risk_score=row.risk_score,
                commit_sha=sha,
                details_url=summary["details_url"],
            )
        client: GitHubAppClient | None = request.app.state.github_app
        if client is not None:
            installation = await installation_for_repository(session, ctx, repository)
            if installation is not None:
                check_run = (installation.installation_id, summary)

    log.info(
        "risk assessment created",
        extra={
            "assessment_id": str(row.id),
            "model_version": row.model_version,
            "risk_band": row.risk_band,
            "cold_start": row.cold_start,
            "history_used": row.history_used,
        },
    )
    if check_run is not None:
        background.add_task(
            _post_check_run, request.app.state.github_app, settings, repository, sha, *check_run
        )
    return RiskAssessmentOut.model_validate(summary)


async def _post_check_run(
    client: GitHubAppClient,
    settings: Settings,
    repository: str,
    head_sha: str,
    installation_id: int,
    summary: dict[str, Any],
) -> None:
    """After the response: a failed Check Run never affects the assessment."""
    payload = check_run_payload(summary, name=settings.github_check_run_name, head_sha=head_sha)
    try:
        await client.create_check_run(installation_id, repository, payload)
    except Exception as exc:
        log.warning(
            "check run not created",
            extra={"repository": repository, "installation_id": installation_id, "error": str(exc)},
        )


# ----------------------------------------------------------------------------- reads
def _encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> AssessmentCursor:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(data, dict):
            raise TypeError("cursor must be an object")
        t, row_id = data.get("t"), data.get("id")
        if not isinstance(t, str) or not isinstance(row_id, str):
            raise TypeError("cursor fields must be strings")
        created = datetime.fromisoformat(t)
        if created.tzinfo is None:
            raise ValueError("naive timestamp")
        return AssessmentCursor(created_at=created.astimezone(UTC), id=uuid.UUID(row_id))
    except (binascii.Error, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ApiError(400, "invalid_cursor", "cursor is malformed") from exc


@router.get(
    "/assessments",
    response_model=RiskAssessmentPage,
    summary="List risk assessments, newest first",
    responses=_ERRORS,
)
async def list_assessments_route(
    request: Request,
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
    repository: Annotated[str | None, Query(max_length=201)] = None,
    commit_sha: Annotated[str | None, Query(max_length=64)] = None,
    risk_band: Annotated[Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> RiskAssessmentPage:
    settings = _settings(request)
    rows = await list_assessments(
        session,
        ctx,
        limit=limit + 1,
        repository=repository.lower() if repository else None,
        commit_sha=commit_sha.lower() if commit_sha else None,
        risk_band=risk_band,
        after=_decode_cursor(cursor) if cursor else None,
    )
    page, more = rows[:limit], len(rows) > limit
    return RiskAssessmentPage(
        items=[
            RiskAssessmentOut.model_validate(service.assessment_summary(r, settings.web_base_url))
            for r in page
        ],
        next_cursor=_encode_cursor(page[-1].created_at, page[-1].id) if more else None,
    )


@router.get(
    "/assessments/{assessment_id}",
    response_model=RiskAssessmentDetail,
    summary="One risk assessment with its features and SHAP vector",
    responses={**_ERRORS, 404: {"description": "No such assessment for this tenant"}},
)
async def get_assessment_route(
    assessment_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RiskAssessmentDetail:
    row = await get_assessment(session, ctx, assessment_id)
    if row is None:
        raise ApiError(404, "not_found", "risk assessment not found")
    return RiskAssessmentDetail.model_validate(
        service.assessment_detail(row, _settings(request).web_base_url)
    )


class ModelInfo(BaseModel):
    role: str
    model_version: str
    checksum: str
    features: int
    probability_exposed: bool
    loaded_at: datetime


class ModelStatus(BaseModel):
    loaded: dict[str, ModelInfo]
    configured: dict[str, bool]


@router.get(
    "/model",
    response_model=ModelStatus,
    summary="The risk models this API process serves",
    responses=_ERRORS,
)
async def model_status(
    request: Request, ctx: Annotated[TenantContext, Depends(require_tenant_read)]
) -> ModelStatus:
    status = _store(request).status()
    return ModelStatus(
        loaded={k: ModelInfo.model_validate(v) for k, v in status["loaded"].items()},
        configured=status["configured"],
    )
