"""CI runs and risk assessments (tenant-scoped, ADR-0010).

:func:`load_history` is the serving side of rl_core's history rule. It returns what
:func:`rl_core.features.online.bounded_history` returns for the same runs: the ``limit`` most
recent *eligible* runs (an outcome, finished strictly before the request) in finish order, plus
the latest run that started before the request, and the exact :class:`HistoryTotals`. Ties in
finish time are broken by ``id`` (UUIDv7, i.e. insertion order), the "input order" of the pure
function. ``rl_db/tests/test_risk_repository.py`` holds the two implementations together.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, literal_column, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.features.online import HistoryTotals, PastRun
from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.risk import (
    CI_RUN_SOURCES,
    CI_RUN_STATUSES,
    FAILED_STATUSES,
    FINISHED_WITH_OUTCOME,
    CiRun,
    RiskAssessment,
)

DEFAULT_HISTORY_LIMIT: Final = 200
FULL_SHA_LEN: Final = 40
_HEX_SHA: Final = re.compile(r"[0-9a-f]{7,64}")
MIN_HISTORY_LIMIT: Final = 20  # the longest history window (fail_rate_last_20, z-score)


# ----------------------------------------------------------------------------- ci_runs
@dataclass(frozen=True, slots=True)
class CiRunWrite:
    repository: str
    commit_sha: str
    started_at: datetime
    status: str
    source: str
    external_id: str
    finished_at: datetime | None = None
    duration_s: float | None = None
    branch: str | None = None
    is_pr: bool = False
    pull_request: int | None = None

    def __post_init__(self) -> None:
        if self.status not in CI_RUN_STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if self.source not in CI_RUN_SOURCES:
            raise ValueError(f"unknown source {self.source!r}")
        if self.repository != self.repository.lower():
            raise ValueError("repository must be lower-case")
        if self.started_at.tzinfo is None or (
            self.finished_at is not None and self.finished_at.tzinfo is None
        ):
            raise ValueError("timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CiRunUpsert:
    """``created``: a new row. ``updated``: a running row got its outcome. ``duplicate``: the
    same run is already recorded (finished, or reported by another source)."""

    id: uuid.UUID | None
    outcome: str  # created | updated | duplicate


async def upsert_ci_run(session: AsyncSession, ctx: TenantContext, run: CiRunWrite) -> CiRunUpsert:
    """Record a CI run idempotently on ``(tenant, source, external_id)``.

    A redelivery of a finished run changes nothing. A ``running`` row is completed by a later
    report of the same run. The same ``external_id`` already reported by *another* source
    (the GitHub App and a CI script both reporting one workflow run) is not recorded twice.
    """
    other = await session.execute(
        select(CiRun.id).where(
            CiRun.tenant_id == ctx.tenant_id,
            CiRun.repository == run.repository,
            CiRun.external_id == run.external_id,
            CiRun.source != run.source,
        )
    )
    existing_other = other.scalars().first()
    if existing_other is not None:
        return CiRunUpsert(id=existing_other, outcome="duplicate")
    duration = run.duration_s
    if duration is None and run.finished_at is not None:
        duration = max((run.finished_at - run.started_at).total_seconds(), 0.0)
    values: dict[str, Any] = {
        "id": uuid7(),
        "tenant_id": ctx.tenant_id,
        "repository": run.repository,
        "commit_sha": run.commit_sha,
        "branch": run.branch,
        "is_pr": run.is_pr,
        "pull_request": run.pull_request,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "duration_s": duration,
        "source": run.source,
        "external_id": run.external_id,
    }
    ins = pg_insert(CiRun).values(values)
    stmt: Any = ins.on_conflict_do_update(
        index_elements=["tenant_id", "source", "external_id"],
        set_={
            "status": ins.excluded.status,
            "finished_at": ins.excluded.finished_at,
            "duration_s": ins.excluded.duration_s,
            "started_at": ins.excluded.started_at,
            "updated_at": func.now(),
        },
        # Only an unfinished row may change: an out-of-order "running" never undoes an outcome.
        where=(CiRun.status == "running") & (ins.excluded.status != "running"),
    ).returning(CiRun.id, literal_column("(xmax = 0)").label("inserted"))
    row = (await session.execute(stmt)).first()
    if row is None:
        found = await session.execute(
            select(CiRun.id).where(
                CiRun.tenant_id == ctx.tenant_id,
                CiRun.source == run.source,
                CiRun.external_id == run.external_id,
            )
        )
        return CiRunUpsert(id=found.scalar_one_or_none(), outcome="duplicate")
    return CiRunUpsert(id=row[0], outcome="created" if row[1] else "updated")


def _past_run(started_at: datetime, finished_at: datetime | None, status: str, dur: Any) -> PastRun:
    failed: bool | None
    if status in FAILED_STATUSES:
        failed = True
    elif status == "passed":
        failed = False
    else:
        failed = None
    return PastRun(
        started_at=started_at,
        finished_at=finished_at,
        failed=failed,
        duration_s=None if dur is None else float(dur),
    )


async def load_history(
    session: AsyncSession,
    ctx: TenantContext,
    repository: str,
    before: datetime,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> tuple[list[PastRun], HistoryTotals]:
    """The repository's build history as seen at ``before`` (module docstring)."""
    if limit < MIN_HISTORY_LIMIT:
        raise ValueError(f"limit must be >= {MIN_HISTORY_LIMIT}")
    eligible = (
        CiRun.tenant_id == ctx.tenant_id,
        CiRun.repository == repository,
        CiRun.status.in_(FINISHED_WITH_OUTCOME),
        CiRun.finished_at < before,
    )
    recent = (
        await session.execute(
            select(CiRun.id, CiRun.started_at, CiRun.finished_at, CiRun.status, CiRun.duration_s)
            .where(*eligible)
            .order_by(CiRun.finished_at.desc(), CiRun.id.desc())
            .limit(limit)
        )
    ).all()[::-1]
    window = [_past_run(r.started_at, r.finished_at, r.status, r.duration_s) for r in recent]

    latest_start = (
        await session.execute(
            select(func.max(CiRun.started_at)).where(
                CiRun.tenant_id == ctx.tenant_id,
                CiRun.repository == repository,
                CiRun.started_at < before,
            )
        )
    ).scalar_one_or_none()
    if latest_start is not None:
        window.append(
            PastRun(started_at=latest_start, finished_at=None, failed=None, duration_s=None)
        )

    if len(recent) < limit:  # the window is the whole eligible history
        fail_pos = [i for i, r in enumerate(recent) if r.status in FAILED_STATUSES]
        since = None if not fail_pos else len(recent) - 1 - fail_pos[-1]
        return window, HistoryTotals(eligible=len(recent), since_last_failure=since)

    total = int(
        (
            await session.execute(select(func.count()).select_from(CiRun).where(*eligible))
        ).scalar_one()
    )
    last_fail = (
        await session.execute(
            select(CiRun.finished_at, CiRun.id)
            .where(*eligible, CiRun.status.in_(FAILED_STATUSES))
            .order_by(CiRun.finished_at.desc(), CiRun.id.desc())
            .limit(1)
        )
    ).first()
    since_last: int | None = None
    if last_fail is not None:
        since_last = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CiRun)
                    .where(
                        *eligible,
                        tuple_(CiRun.finished_at, CiRun.id)
                        > tuple_(last_fail.finished_at, last_fail.id),
                    )
                )
            ).scalar_one()
        )
    return window, HistoryTotals(eligible=total, since_last_failure=since_last)


async def list_ci_runs(
    session: AsyncSession,
    ctx: TenantContext,
    repository: str | None = None,
    *,
    commit_sha: str | None = None,
    limit: int = 50,
) -> list[CiRun]:
    """The tenant's CI runs of a repository and/or a commit, newest start first. At least one
    filter is required (a tenant-wide listing is not what this serves).

    ``commit_sha`` (lower-case hex) of 40+ characters matches exactly; a shorter one, an
    abbreviated SHA as deployment events may carry, matches runs whose SHA starts with it.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be within 1..1000")
    if repository is None and commit_sha is None:
        raise ValueError("filter by repository, commit_sha or both")
    stmt = select(CiRun).where(CiRun.tenant_id == ctx.tenant_id)
    if repository is not None:
        stmt = stmt.where(CiRun.repository == repository)
    if commit_sha is not None:
        if not _HEX_SHA.fullmatch(commit_sha):
            raise ValueError("commit_sha must be 7-64 lower-case hex characters")
        stmt = stmt.where(
            CiRun.commit_sha == commit_sha
            if len(commit_sha) >= FULL_SHA_LEN
            else CiRun.commit_sha.startswith(commit_sha, autoescape=True)
        )
    stmt = stmt.order_by(CiRun.started_at.desc(), CiRun.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


# ----------------------------------------------------------------------------- assessments
def idempotency_key(
    ctx: TenantContext, repository: str, commit_sha: str, event: str, pull_request: int | None
) -> str:
    return f"risk:{ctx.tenant_id}:{repository}:{commit_sha}:{event}:{pull_request or 0}"


async def lock_idempotency_key(session: AsyncSession, key: str) -> None:
    """Transaction-scoped advisory lock: concurrent identical requests are serialised, so the
    second one sees the first one's row instead of inserting a duplicate."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def find_recent_assessment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    repository: str,
    commit_sha: str,
    event: str,
    pull_request: int | None,
    since: datetime,
) -> RiskAssessment | None:
    """The newest assessment of the same (repository, commit, event, PR) created at or after
    ``since``, or None."""
    stmt = select(RiskAssessment).where(
        RiskAssessment.tenant_id == ctx.tenant_id,
        RiskAssessment.repository == repository,
        RiskAssessment.commit_sha == commit_sha,
        RiskAssessment.event == event,
        RiskAssessment.created_at >= since,
    )
    stmt = stmt.where(
        RiskAssessment.pull_request.is_(None)
        if pull_request is None
        else RiskAssessment.pull_request == pull_request
    )
    stmt = stmt.order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_assessment(
    session: AsyncSession, ctx: TenantContext, values: Mapping[str, Any]
) -> RiskAssessment:
    if "tenant_id" in values:
        raise ValueError("tenant_id comes from the context")
    row = RiskAssessment(id=values.get("id") or uuid7(), tenant_id=ctx.tenant_id)
    for key, value in values.items():
        if key != "id":
            setattr(row, key, value)
    session.add(row)
    await session.flush()
    await session.refresh(row, ["created_at"])
    return row


async def get_assessment(
    session: AsyncSession, ctx: TenantContext, assessment_id: uuid.UUID
) -> RiskAssessment | None:
    """The tenant's assessment, or None (another tenant's id looks missing, ADR-0010 §4)."""
    stmt = select(RiskAssessment).where(
        RiskAssessment.tenant_id == ctx.tenant_id, RiskAssessment.id == assessment_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class AssessmentCursor:
    created_at: datetime
    id: uuid.UUID


async def list_assessments(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int,
    repository: str | None = None,
    commit_sha: str | None = None,
    risk_band: str | None = None,
    after: AssessmentCursor | None = None,
    since: datetime | None = None,
) -> list[RiskAssessment]:
    """Newest first by ``(created_at, id)``; keyset pagination. ``since`` keeps assessments
    created at or after it."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be within 1..1000")
    stmt = select(RiskAssessment).where(RiskAssessment.tenant_id == ctx.tenant_id)
    if repository is not None:
        stmt = stmt.where(RiskAssessment.repository == repository)
    if commit_sha is not None:
        stmt = stmt.where(RiskAssessment.commit_sha == commit_sha)
    if risk_band is not None:
        stmt = stmt.where(RiskAssessment.risk_band == risk_band)
    if since is not None:
        stmt = stmt.where(RiskAssessment.created_at >= since)
    if after is not None:
        stmt = stmt.where(
            tuple_(RiskAssessment.created_at, RiskAssessment.id)
            < tuple_(after.created_at, after.id)
        )
    stmt = stmt.order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())
