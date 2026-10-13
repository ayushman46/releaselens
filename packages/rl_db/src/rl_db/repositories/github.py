"""GitHub App data of one tenant (ADR-0010): its installations and pull requests.

How a webhook delivery finds its tenant in the first place (installation id → tenant) is a
cross-tenant lookup and lives in :mod:`rl_db.admin.github`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.github import GithubInstallation, GithubPullRequest
from rl_db.repositories._tenancy import tenant_scoped


@tenant_scoped
async def installation_for_repository(
    session: AsyncSession, ctx: TenantContext, repository: str
) -> GithubInstallation | None:
    """The tenant's active installation on the repository's owner account, if any.

    An installation can be limited to selected repositories; GitHub then refuses a token for
    other repositories, and the Check Run call fails (logged, never surfaced to the caller).
    """
    owner = repository.split("/", 1)[0].lower()
    stmt = (
        select(GithubInstallation)
        .where(
            GithubInstallation.tenant_id == ctx.tenant_id,
            GithubInstallation.account_login == owner,
            GithubInstallation.suspended_at.is_(None),
        )
        .order_by(GithubInstallation.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class PullRequestWrite:
    repository: str
    number: int
    head_sha: str
    base_sha: str | None
    base_ref: str | None
    state: str
    merged: bool
    draft: bool
    github_updated_at: datetime


@tenant_scoped
async def upsert_pull_request(
    session: AsyncSession, ctx: TenantContext, pr: PullRequestWrite
) -> None:
    """Keep the newest state of a pull request (an older, re-delivered event never wins)."""
    ins = pg_insert(GithubPullRequest).values(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        repository=pr.repository,
        number=pr.number,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        base_ref=pr.base_ref,
        state=pr.state,
        merged=pr.merged,
        draft=pr.draft,
        github_updated_at=pr.github_updated_at,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=["tenant_id", "repository", "number"],
        set_={
            "head_sha": ins.excluded.head_sha,
            "base_sha": ins.excluded.base_sha,
            "base_ref": ins.excluded.base_ref,
            "state": ins.excluded.state,
            "merged": ins.excluded.merged,
            "draft": ins.excluded.draft,
            "github_updated_at": ins.excluded.github_updated_at,
            "received_at": func.now(),
        },
        where=GithubPullRequest.github_updated_at <= ins.excluded.github_updated_at,
    )
    await session.execute(stmt)


@tenant_scoped
async def get_pull_request(
    session: AsyncSession, ctx: TenantContext, repository: str, number: int
) -> GithubPullRequest | None:
    stmt = select(GithubPullRequest).where(
        GithubPullRequest.tenant_id == ctx.tenant_id,
        GithubPullRequest.repository == repository,
        GithubPullRequest.number == number,
    )
    return (await session.execute(stmt)).scalar_one_or_none()
