"""Analysis job handler: ``early_5m`` / ``early_10m`` / ``final`` → ``regression_analyses`` (M8).

For a claimed job:

1. Load the deployment (tenant-scoped; a deployment deleted since is a no-op).
2. Resolve the :class:`~rl_core.regression.windows.WindowConfig` for its environment: the spec
   defaults (baseline 30 min, warm-up 2 min, post window to T+15 min), or a per-environment
   profile from settings (``demo`` uses 10 / 1 / 8 minutes by default, so a demo deploy shows a
   verdict sooner). The windows used are stored with the result.
3. Choose ``as_of``: early kinds analyse the post window **as of their scheduled time**
   (``deployed_at`` + 5 or 10 minutes, never later than the actual run time), so a run that is
   delayed or retried still answers "what did we know at T+5"; ``final`` analyses the full window
   and refuses to run before it has closed (:class:`AnalysisNotDueError`, retried by the runner).
4. Load the tenant's spans of that environment in ``[baseline start, effective post end]``
   (plus a short tail, see :mod:`rl_db.repositories.analyses`), bounded by ``max_spans`` with
   deterministic trace-hash sampling beyond it.
5. Run :func:`rl_core.regression.analysis.analyse_deployment` in a worker thread (CPU-bound; the
   event loop keeps consuming meanwhile).
6. In **one transaction**: upsert the ``(deployment, kind)`` row and add the
   ``analysis.results`` outbox message (:mod:`rl_core.events.analysis`, key
   ``tenant_id:deployment_id``). Either both are written or neither.

A job re-run (retry after a crash between the commit and the job's completion) replaces the row
and emits another message; consumers are idempotent per ``(tenant, deployment, kind)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_core.events.analysis import (
    AnalysisSummary,
    analysis_key,
    encode_analysis,
    make_analysis_envelope,
)
from rl_core.events.topics import ANALYSIS_RESULTS
from rl_core.regression.analysis import RegressionAnalysis, analyse_deployment
from rl_core.regression.evidence import DeploymentRef, ServiceStatus
from rl_core.regression.windows import AnalysisWindows, WindowConfig
from rl_db.admin.jobs import ClaimedJob
from rl_db.models import Deployment, JobKind
from rl_db.repositories import (
    AnalysisWrite,
    add_outbox_event,
    analysis_schedule,
    get_deployment,
    load_analysis_spans,
    upsert_analysis,
)
from rl_db.repositories.analyses import LOAD_TAIL_MARGIN

log = logging.getLogger("rl_worker.analysis")

#: Shorter windows for the demo environment (minutes): a demo deploy shows a verdict at T+8.
DEMO_WINDOWS: Mapping[str, int] = {
    "baseline_minutes": 10,
    "warmup_minutes": 1,
    "post_end_minutes": 8,
}

DEFAULT_MAX_SPANS = 200_000


class AnalysisNotDueError(Exception):
    """The final analysis ran before its post window closed; the runner retries it later."""


@dataclass(frozen=True)
class AnalysisSettings:
    default: WindowConfig = field(default_factory=WindowConfig)
    per_environment: Mapping[str, WindowConfig] = field(default_factory=dict)
    max_spans: int = DEFAULT_MAX_SPANS
    schedule: Sequence[tuple[JobKind, timedelta]] = field(default_factory=analysis_schedule)

    def __post_init__(self) -> None:
        if self.max_spans < 1:
            raise ValueError("max_spans must be positive")
        final = dict(self.schedule).get(JobKind.FINAL)
        if final is None:
            raise ValueError("the schedule must include the final analysis")
        for name, cfg in {"(default)": self.default, **self.per_environment}.items():
            if timedelta(minutes=cfg.post_end_minutes) > final:
                raise ValueError(
                    f"environment {name}: post window ends at T+{cfg.post_end_minutes} min, "
                    f"after the final analysis runs (T+{final})"
                )

    def config_for(self, environment: str) -> tuple[str, WindowConfig]:
        if environment in self.per_environment:
            return environment, self.per_environment[environment]
        return "default", self.default

    def offset(self, kind: JobKind) -> timedelta:
        return dict(self.schedule)[kind]


def deployed_service_status(analysis: RegressionAnalysis, service: str) -> ServiceStatus:
    """Status of the deployed service itself (the ``analysis.results`` contract's ``status``)."""
    for evidence in analysis.services:
        if evidence.service == service:
            return evidence.status
    return ServiceStatus.INSUFFICIENT_DATA


def summarize(
    analysis: RegressionAnalysis,
    deployment: Deployment,
    *,
    analysis_id: Any,
    kind: JobKind,
    completed_at: datetime,
) -> AnalysisSummary:
    triage = analysis.triage
    radius = analysis.blast_radius
    return AnalysisSummary(
        deployment_id=deployment.id,
        analysis_id=analysis_id,
        kind=kind.value,
        service=deployment.service,
        environment=deployment.environment,
        version=deployment.version,
        previous_version=deployment.previous_version,
        status=deployed_service_status(analysis, deployment.service).value,
        severity_score=None if analysis.severity is None else analysis.severity.score,
        severity_band=None if analysis.severity is None else analysis.severity.band.value,
        probable_origin=None if triage is None else triage.probable_origin,
        confidence=None if triage is None else triage.confidence.value,
        affected_services=[] if radius is None else list(radius.affected),
        engine_version=analysis.engine_version,
        completed_at=completed_at,
    )


def _windows_json(windows: AnalysisWindows, profile: str, cfg: WindowConfig) -> dict[str, Any]:
    return {
        "profile": profile,
        "baseline_minutes": cfg.baseline_minutes,
        "warmup_minutes": cfg.warmup_minutes,
        "post_end_minutes": cfg.post_end_minutes,
        **windows.model_dump(mode="json"),
    }


class AnalysisJobHandler:
    """Callable registered for every analysis :class:`JobKind` (module docstring)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: AnalysisSettings | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self.settings = settings or AnalysisSettings()
        self._clock = clock

    def as_of(self, kind: JobKind, deployed_at: datetime, now: datetime) -> datetime | None:
        if kind is JobKind.FINAL:
            return None
        return max(deployed_at, min(deployed_at + self.settings.offset(kind), now))

    async def __call__(self, job: ClaimedJob) -> None:
        ctx = job.tenant_context()
        async with self._sessions() as session:
            deployment = await get_deployment(session, ctx, job.deployment_id)
        if deployment is None:
            log.info("analysis skipped: the deployment no longer exists")
            return
        profile, cfg = self.settings.config_for(deployment.environment)
        now = self._clock()
        as_of = self.as_of(job.kind, deployment.deployed_at, now)
        windows = cfg.windows(deployment.deployed_at, as_of)
        if job.kind is JobKind.FINAL and now < windows.post_end:
            raise AnalysisNotDueError(
                f"post window closes at {windows.post_end.isoformat()}; it is {now.isoformat()}"
            )

        async with self._sessions() as session:
            load = await load_analysis_spans(
                session,
                ctx,
                environment=deployment.environment,
                start=windows.baseline_start,
                end=windows.effective_post_end,
                max_spans=self.settings.max_spans,
            )
        ref = DeploymentRef(
            service=deployment.service,
            environment=deployment.environment,
            version=deployment.version,
            previous_version=deployment.previous_version,
            deployed_at=deployment.deployed_at,
            tenant_id=str(ctx.tenant_id),
        )
        analysis = await asyncio.to_thread(analyse_deployment, load.spans, ref, cfg, as_of=as_of)

        triage = analysis.triage
        completed_at = self._clock()
        async with self._sessions.begin() as session:
            analysis_id = await upsert_analysis(
                session,
                ctx,
                AnalysisWrite(
                    deployment_id=deployment.id,
                    kind=job.kind,
                    status=analysis.status.value,
                    severity_score=None if analysis.severity is None else analysis.severity.score,
                    severity_band=(
                        None if analysis.severity is None else analysis.severity.band.value
                    ),
                    probable_origin=None if triage is None else triage.probable_origin,
                    confidence=None if triage is None else triage.confidence.value,
                    result=analysis.model_dump(mode="json"),
                    engine_version=analysis.engine_version,
                    windows=_windows_json(analysis.windows, profile, cfg),
                    sampling=load.describe(
                        windows.baseline_start, windows.effective_post_end + LOAD_TAIL_MARGIN
                    ),
                ),
            )
            summary = summarize(
                analysis,
                deployment,
                analysis_id=analysis_id,
                kind=job.kind,
                completed_at=completed_at,
            )
            envelope = make_analysis_envelope(tenant_id=ctx.tenant_id, summary=summary)
            await add_outbox_event(
                session,
                ctx,
                ANALYSIS_RESULTS,
                analysis_key(ctx.tenant_id, deployment.id),
                json.loads(encode_analysis(envelope)),
                headers={
                    "rl-event-type": envelope.type,
                    "rl-schema-version": str(envelope.schema_version),
                },
            )
        log.info(
            "regression analysis stored",
            extra={
                "deployment_id": str(deployment.id),
                "analysis_id": str(analysis_id),
                "kind": job.kind.value,
                "status": analysis.status.value,
                "probable_origin": summary.probable_origin,
                "confidence": summary.confidence,
                "spans_loaded": len(load.spans),
                "sampled": load.sampled,
                "profile": profile,
            },
        )
