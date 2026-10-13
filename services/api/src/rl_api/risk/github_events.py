"""What a verified, deduplicated GitHub App delivery changes, in its tenant's transaction.

==================  =========================================================================
event               effect
==================  =========================================================================
``workflow_run``    a ``ci_runs`` row (source ``github_app``, ``external_id`` =
                    ``gha:<run id>:<attempt>``): ``requested``/``in_progress`` → running,
                    ``completed`` → passed / failed / errored / canceled. Only runs triggered
                    by ``push``, ``pull_request``, ``pull_request_target`` or ``merge_group``
                    are builds; scheduled or manual workflows are ignored.
``check_suite``     ``completed`` suites of *other* CI apps (``check_suite:<id>``). GitHub
                    Actions suites are skipped (``workflow_run`` covers them) and so are this
                    App's own suites (its Check Runs are not builds).
``pull_request``    the PR's latest state (``github_pull_requests``; no title, body or author).
``deployment_status`` ``success`` → a ``deployment.completed`` event through the outbox, when
                    the repository's project has exactly one service (else ignored).
``installation``    ``deleted`` / ``suspend`` → the link is suspended; ``unsuspend`` lifts it.
==================  =========================================================================

Conclusions map to ADR-0005 statuses: ``success`` → passed, ``failure`` → failed,
``timed_out`` / ``startup_failure`` → errored, anything else (``cancelled``, ``skipped``,
``neutral``, ``stale``, ``action_required``) → canceled, which never counts as an outcome.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.events import DEPLOYMENT_EVENTS, deployment_key, encode_envelope, make_envelope
from rl_core.events.deployment import MAX_REPOSITORY_LEN, DeploymentPayloadV2
from rl_core.tenancy import TenantContext
from rl_db.admin.github import set_installation_suspended
from rl_db.repositories.github import PullRequestWrite, upsert_pull_request
from rl_db.repositories.outbox import add_outbox_event
from rl_db.repositories.projects import find_project_by_repository, list_services
from rl_db.repositories.risk import CiRunWrite, upsert_ci_run

log = logging.getLogger("rl_api.github_events")

BUILD_EVENTS: Final = frozenset({"push", "pull_request", "pull_request_target", "merge_group"})
CONCLUSIONS: Final = {
    "success": "passed",
    "failure": "failed",
    "timed_out": "errored",
    "startup_failure": "errored",
}
# uuid5 namespace for deployment events derived from GitHub deployment statuses.
DEPLOYMENT_NS: Final = uuid.UUID("6c1f2f8e-6a57-5b8e-9d0a-1f6f0b8d3c51")


class MalformedPayloadError(ValueError):
    pass


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _str(d: Any, *path: str, required: bool = True) -> str | None:
    v = _get(d, *path)
    if isinstance(v, str) and v:
        return v
    if required:
        raise MalformedPayloadError(f"missing {'.'.join(path)}")
    return None


def _int(d: Any, *path: str) -> int:
    v = _get(d, *path)
    if isinstance(v, bool) or not isinstance(v, int):
        raise MalformedPayloadError(f"missing {'.'.join(path)}")
    return v


def _time(d: Any, *path: str, required: bool = True) -> datetime | None:
    raw = _str(d, *path, required=required)
    if raw is None:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedPayloadError(f"bad timestamp {'.'.join(path)}") from exc
    if t.tzinfo is None:
        raise MalformedPayloadError(f"naive timestamp {'.'.join(path)}")
    return t.astimezone(UTC)


def _repository(payload: dict[str, Any]) -> str:
    full = _str(payload, "repository", "full_name")
    assert full is not None
    if full.count("/") != 1 or len(full) > MAX_REPOSITORY_LEN:
        raise MalformedPayloadError("repository.full_name is not owner/repo")
    return full.lower()


def _status(conclusion: str | None) -> str:
    return CONCLUSIONS.get(conclusion or "", "canceled")


def _first_pr(items: Any) -> int | None:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        n = items[0].get("number")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            return n
    return None


async def handle_event(
    session: AsyncSession,
    ctx: TenantContext,
    event: str,
    payload: dict[str, Any],
    *,
    app_id: str | None,
) -> str:
    """Apply one delivery; returns a short outcome for the delivery log."""
    action = payload.get("action")
    if event == "workflow_run":
        return await _workflow_run(session, ctx, payload, action)
    if event == "check_suite":
        return await _check_suite(session, ctx, payload, action, app_id)
    if event == "pull_request":
        return await _pull_request(session, ctx, payload)
    if event == "deployment_status":
        return await _deployment_status(session, ctx, payload)
    if event == "installation":
        inst = _int(payload, "installation", "id")
        if action in ("deleted", "suspend"):
            await set_installation_suspended(session, inst, suspended_at=datetime.now(UTC))
            return f"installation:{action}"
        if action == "unsuspend":
            await set_installation_suspended(session, inst, suspended_at=None)
            return "installation:unsuspend"
        return f"ignored:installation_{action}"
    return f"ignored:event_{event}"


async def _workflow_run(
    session: AsyncSession, ctx: TenantContext, payload: dict[str, Any], action: Any
) -> str:
    run = payload.get("workflow_run")
    if not isinstance(run, dict):
        raise MalformedPayloadError("missing workflow_run")
    if run.get("event") not in BUILD_EVENTS:
        return f"ignored:trigger_{run.get('event')}"
    if action not in ("requested", "in_progress", "completed"):
        return f"ignored:action_{action}"
    started = _time(run, "run_started_at", required=False) or _time(run, "created_at")
    assert started is not None
    finished = _time(run, "updated_at") if action == "completed" else None
    status = _status(run.get("conclusion")) if action == "completed" else "running"
    if finished is not None and finished < started:
        finished = started
    attempt = run.get("run_attempt") if isinstance(run.get("run_attempt"), int) else 1
    pr = _first_pr(run.get("pull_requests"))
    result = await upsert_ci_run(
        session,
        ctx,
        CiRunWrite(
            repository=_repository(payload),
            commit_sha=(_str(run, "head_sha") or "").lower()[:64],
            started_at=started,
            finished_at=finished,
            status=status,
            source="github_app",
            external_id=f"gha:{_int(run, 'id')}:{attempt}",
            branch=(_str(run, "head_branch", required=False) or "")[:255] or None,
            is_pr=str(run.get("event", "")).startswith("pull_request") or pr is not None,
            pull_request=pr,
        ),
    )
    return f"ci_run:{result.outcome}"


async def _check_suite(
    session: AsyncSession,
    ctx: TenantContext,
    payload: dict[str, Any],
    action: Any,
    app_id: str | None,
) -> str:
    suite = payload.get("check_suite")
    if not isinstance(suite, dict):
        raise MalformedPayloadError("missing check_suite")
    if action != "completed":
        return f"ignored:action_{action}"
    app_raw = suite.get("app")
    app: dict[str, Any] = app_raw if isinstance(app_raw, dict) else {}
    if app.get("slug") == "github-actions":
        return "ignored:github_actions_suite"  # counted through workflow_run
    if app_id is not None and str(app.get("id")) == str(app_id):
        return "ignored:own_suite"
    started = _time(suite, "created_at")
    finished = _time(suite, "updated_at")
    assert started is not None
    assert finished is not None
    pr = _first_pr(suite.get("pull_requests"))
    result = await upsert_ci_run(
        session,
        ctx,
        CiRunWrite(
            repository=_repository(payload),
            commit_sha=(_str(suite, "head_sha") or "").lower()[:64],
            started_at=started,
            finished_at=max(finished, started),
            status=_status(suite.get("conclusion")),
            source="github_app",
            external_id=f"check_suite:{_int(suite, 'id')}",
            branch=(_str(suite, "head_branch", required=False) or "")[:255] or None,
            is_pr=pr is not None,
            pull_request=pr,
        ),
    )
    return f"ci_run:{result.outcome}"


async def _pull_request(session: AsyncSession, ctx: TenantContext, payload: dict[str, Any]) -> str:
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        raise MalformedPayloadError("missing pull_request")
    state = pr.get("state")
    if state not in ("open", "closed"):
        raise MalformedPayloadError("pull_request.state")
    updated = _time(pr, "updated_at")
    assert updated is not None
    await upsert_pull_request(
        session,
        ctx,
        PullRequestWrite(
            repository=_repository(payload),
            number=_int(pr, "number"),
            head_sha=(_str(pr, "head", "sha") or "").lower()[:64],
            base_sha=(_str(pr, "base", "sha", required=False) or "").lower()[:64] or None,
            base_ref=(_str(pr, "base", "ref", required=False) or "")[:255] or None,
            state=state,
            merged=bool(pr.get("merged")),
            draft=bool(pr.get("draft")),
            github_updated_at=updated,
        ),
    )
    return "pull_request:stored"


async def _deployment_status(
    session: AsyncSession, ctx: TenantContext, payload: dict[str, Any]
) -> str:
    status = payload.get("deployment_status")
    deployment = payload.get("deployment")
    if not isinstance(status, dict) or not isinstance(deployment, dict):
        raise MalformedPayloadError("missing deployment_status / deployment")
    if status.get("state") != "success":
        return f"ignored:state_{status.get('state')}"
    repository = _repository(payload)
    project = await find_project_by_repository(session, ctx, repository)
    if project is None:
        return "ignored:no_project"
    services = await list_services(session, ctx, project.id)
    if len(services) != 1:
        return "ignored:service_ambiguous" if services else "ignored:no_service"
    sha = (_str(deployment, "sha") or "").lower()
    deployed_at = _time(status, "created_at")
    assert deployed_at is not None
    event_id = uuid.uuid5(DEPLOYMENT_NS, f"github-deployment-status:{_int(status, 'id')}")
    try:
        body = DeploymentPayloadV2.model_validate_json(
            json.dumps(
                {
                    "service": services[0].name,
                    "environment": _str(deployment, "environment"),
                    "version": sha,
                    "commit_sha": sha,
                    "deployed_at": deployed_at.isoformat(),
                    "repository": repository,
                    "labels": {"source": "github_app"},
                }
            )
        )
    except ValueError as exc:  # pydantic ValidationError: e.g. an environment name we reject
        raise MalformedPayloadError(f"deployment does not fit the event schema: {exc}") from exc
    envelope = make_envelope(tenant_id=ctx.tenant_id, payload=body, event_id=event_id)
    await add_outbox_event(
        session,
        ctx,
        DEPLOYMENT_EVENTS,
        deployment_key(ctx.tenant_id, body.service),
        json.loads(encode_envelope(envelope)),
        headers={"rl-event-type": envelope.type, "rl-schema-version": "2"},
    )
    return "deployment:queued"
