"""CI build history for the risk model: ``POST /api/v1/ci-runs`` and ``GET /api/v1/ci-runs``.

The risk model's history features (previous build failed, recent failure rates, …) come from
the tenant's own finished CI runs. With the GitHub App installed they arrive by webhook
(``workflow_run`` / ``check_suite``). Without it, a CI job reports each finished run here.

Scope: ``risk:write``, the same single-purpose key the GitHub Action already holds (ADR-0016:
one narrow key per integration). Runs only feed the risk model, so a separate scope would
just mean a second secret in every CI system.

Idempotent on ``(source, external_id)``: a retry changes nothing, a ``running`` report is
completed by a later report of the same run, and an outcome is never overwritten.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from rl_api.auth import require_scope
from rl_api.deps import get_session
from rl_api.errors import ApiError
from rl_api.principal import require_tenant_read
from rl_api.ratelimit import limit_api_key
from rl_api.routers.risk import Branch, CommitSha
from rl_core.events.deployment import PullRequestNumber, Repository
from rl_core.tenancy import TenantContext
from rl_db.repositories.risk import CiRunWrite, list_ci_runs, upsert_ci_run

router = APIRouter(prefix="/api/v1/ci-runs", tags=["risk"])

_write = limit_api_key(require_scope("risk:write"), "risk")
MAX_FUTURE_SKEW = timedelta(hours=1)

ExternalId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[!-~]+$")
]


class CiRunCreate(BaseModel):
    """A CI run (build). ``external_id`` identifies the run in the CI system; for GitHub Actions
    use ``gha:<run_id>:<run_attempt>``, the id the GitHub App records, so the two never count one
    run twice."""

    model_config = ConfigDict(extra="forbid")

    repository: Repository
    commit_sha: CommitSha
    external_id: ExternalId
    status: Literal["passed", "failed", "errored", "canceled", "running"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    duration_s: float | None = Field(default=None, ge=0, le=7 * 86_400)
    branch: Branch | None = None
    is_pr: bool = False
    pull_request: PullRequestNumber | None = None
    source: Literal["action", "api"] = "api"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        finished = self.finished_at is not None
        if self.status == "running" and finished:
            raise ValueError("a running build has no finished_at")
        if self.status in ("passed", "failed", "errored") and not finished:
            raise ValueError(f"a {self.status} build needs finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self


class CiRunAccepted(BaseModel):
    id: uuid.UUID | None
    outcome: Literal["created", "updated", "duplicate"]


class CiRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository: str
    commit_sha: str
    branch: str | None
    is_pr: bool
    pull_request: int | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    duration_s: float | None
    source: str
    external_id: str


@router.post(
    "",
    status_code=201,
    response_model=CiRunAccepted,
    summary="Report a CI run (build) for the risk model's history",
    responses={
        200: {"description": "Already recorded (or completed a running run)"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "The API key lacks the risk:write scope"},
        429: {"description": "Rate limit for this API key exceeded"},
    },
)
async def report_ci_run(
    body: CiRunCreate,
    response: Response,
    ctx: Annotated[TenantContext, Depends(_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CiRunAccepted:
    now = datetime.now(UTC)
    if body.started_at > now + MAX_FUTURE_SKEW or (
        body.finished_at is not None and body.finished_at > now + MAX_FUTURE_SKEW
    ):
        raise ApiError(422, "validation_error", "timestamps are more than 1 hour in the future")
    run = CiRunWrite(
        repository=body.repository.lower(),
        commit_sha=body.commit_sha.lower(),
        started_at=body.started_at.astimezone(UTC),
        finished_at=body.finished_at.astimezone(UTC) if body.finished_at else None,
        status=body.status,
        duration_s=body.duration_s,
        source=body.source,
        external_id=body.external_id,
        branch=body.branch,
        is_pr=body.is_pr or body.pull_request is not None,
        pull_request=body.pull_request,
    )
    async with session.begin():
        result = await upsert_ci_run(session, ctx, run)
    if result.outcome != "created":
        response.status_code = 200
    return CiRunAccepted.model_validate({"id": result.id, "outcome": result.outcome})


@router.get(
    "",
    response_model=list[CiRunOut],
    summary="Recent CI runs of a repository, newest start first",
    responses={401: {"description": "Missing or invalid credentials"}},
)
async def list_ci_runs_route(
    ctx: Annotated[TenantContext, Depends(require_tenant_read)],
    session: Annotated[AsyncSession, Depends(get_session)],
    repository: Annotated[str, Query(min_length=3, max_length=201)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CiRunOut]:
    rows = await list_ci_runs(session, ctx, repository.lower(), limit=limit)
    return [CiRunOut.model_validate(r) for r in rows]
