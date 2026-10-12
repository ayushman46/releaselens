"""``analyse_deployment`` — the pure entry point that becomes one ``regression_analyses`` row.

Pipeline (every step deterministic for a given seed)::

    spans ─▶ tenant check, environment filter, de-duplication ─▶ TraceIndex
          ─▶ per-service request samples (latency, service self-time, error, own error)
          ─▶ strict gates per service (+ onset where the p95 latency gate passed)
          ─▶ service graph ─▶ probable-origin scoring ─▶ relaxed gates on reachable neighbours
          ─▶ blast radius ─▶ overall status and severity

**Input hygiene.**

* All spans must carry one ``tenant_id`` (and match ``deployment.tenant_id`` when given): mixing
  tenants raises ``ValueError`` instead of producing a cross-tenant analysis.
* Spans whose ``environment`` is set and differs from the deployment's are dropped; spans with no
  environment are kept (many SDKs do not set ``deployment.environment``, and dropping them would
  turn a whole service into INSUFFICIENT_DATA). Counts are reported in :class:`InputSummary`.
* Duplicate ``(trace_id, span_id)`` deliveries keep the first copy.

**Overall status**: REGRESSED if any service is REGRESSED; else WARNING if any is WARNING; else
INSUFFICIENT_DATA if the deployed service could not be assessed (absent or INSUFFICIENT_DATA) or
no service could; else HEALTHY.

**Overall severity**: the maximum severity v1 score over assessed services (the worst-affected
service, named in ``severity_service``; ties → alphabetical). It describes the impact of the
release, which is often largest at the probable origin but need not be; the origin's own score is
in its evidence. ``None`` when no service could be assessed. Severity is not a probability.

**Blast radius** is computed from the probable origin; with LOW triage confidence (no probable
origin) it is computed from the top-ranked candidate, and ``triage.confidence`` says so.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict

from rl_core.blast_radius import BlastRadius, compute_blast_radius
from rl_core.graph import GraphSnapshot, ServiceGraph
from rl_core.regression.detect import assess_service
from rl_core.regression.evidence import (
    DeploymentRef,
    ServiceEvidence,
    ServiceStatus,
    Severity,
)
from rl_core.regression.metrics import ServiceSamples, build_service_samples, window_stats
from rl_core.regression.onset import estimate_onset, has_latency_onset
from rl_core.regression.windows import AnalysisWindows, WindowConfig
from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import Span
from rl_core.triage import TriageResult, score_origins

ENGINE_VERSION: Final = "regression-engine/1.0.0"


class InputSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spans_received: int
    other_environment_spans_dropped: int
    duplicate_spans_dropped: int
    spans_in_windows: int  # start within [baseline start, effective post end]


class RegressionAnalysis(BaseModel):
    """The complete, JSON-serialisable result of analysing one deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_version: str
    tenant_id: str | None
    deployment: DeploymentRef
    windows: AnalysisWindows
    config: WindowConfig
    relaxed_config: WindowConfig
    seed: int
    status: ServiceStatus
    severity: Severity | None
    severity_service: str | None
    services: tuple[ServiceEvidence, ...]
    relaxed_services: tuple[ServiceEvidence, ...]  # relaxed evidence for blast-radius neighbours
    graph: GraphSnapshot
    triage: TriageResult | None
    blast_radius: BlastRadius | None
    input: InputSummary

    def service(self, name: str) -> ServiceEvidence:
        for ev in self.services:
            if ev.service == name:
                return ev
        raise KeyError(name)


def _single_tenant(spans: list[Span], deployment: DeploymentRef) -> str | None:
    tenants = {s.tenant_id for s in spans}
    if len(tenants) > 1:
        raise ValueError(f"spans from {len(tenants)} tenants passed to one analysis")
    tenant = next(iter(tenants)) if tenants else None
    if deployment.tenant_id is not None and tenant is not None and tenant != deployment.tenant_id:
        raise ValueError("spans belong to a different tenant than the deployment")
    return tenant if tenant is not None else deployment.tenant_id


def _overall_status(evidence: dict[str, ServiceEvidence], deployed: str) -> ServiceStatus:
    statuses = [ev.status for ev in evidence.values()]
    if ServiceStatus.REGRESSED in statuses:
        return ServiceStatus.REGRESSED
    if ServiceStatus.WARNING in statuses:
        return ServiceStatus.WARNING
    deployed_ev = evidence.get(deployed)
    if deployed_ev is None or deployed_ev.status is ServiceStatus.INSUFFICIENT_DATA:
        return ServiceStatus.INSUFFICIENT_DATA
    if all(s is ServiceStatus.INSUFFICIENT_DATA for s in statuses):
        return ServiceStatus.INSUFFICIENT_DATA
    return ServiceStatus.HEALTHY


def _overall_severity(evidence: dict[str, ServiceEvidence]) -> tuple[Severity | None, str | None]:
    best: tuple[Severity, str] | None = None
    for name in sorted(evidence):
        sev = evidence[name].severity
        if sev is not None and (best is None or sev.score > best[0].score):
            best = (sev, name)
    return (None, None) if best is None else best


def analyse_deployment(
    spans: Iterable[Span],
    deployment: DeploymentRef,
    config: WindowConfig | None = None,
    seed: int | None = None,
    *,
    as_of: datetime | None = None,
    relaxed_config: WindowConfig | None = None,
) -> RegressionAnalysis:
    """Analyse one deployment from the spans around it (see module docstring).

    ``seed`` overrides ``config.bootstrap_seed`` when given. ``as_of`` truncates the post window
    for analyses run before ``T + post_end_minutes``. ``relaxed_config`` defaults to
    ``config.relaxed()``.
    """
    cfg = config if config is not None else WindowConfig()
    relaxed_cfg = relaxed_config if relaxed_config is not None else cfg.relaxed()
    used_seed = cfg.bootstrap_seed if seed is None else seed
    windows = cfg.windows(deployment.deployed_at, as_of)

    received = list(spans)
    tenant = _single_tenant(received, deployment)
    env = deployment.environment
    kept = [s for s in received if env is None or s.environment is None or s.environment == env]
    index = TraceIndex.build(kept)

    lo, hi = windows.baseline_start_ns, windows.effective_post_end_ns
    samples = build_service_samples(index, start_ns=lo, end_ns=hi)
    graph = ServiceGraph.from_spans(index, start_ns=lo, end_ns=hi)
    services = sorted(set(graph.nodes) | set(samples))

    per_service: dict[str, ServiceSamples] = {
        s: samples.get(s, ServiceSamples.empty(s)) for s in services
    }
    evidence: dict[str, ServiceEvidence] = {}
    for service in services:
        sample = per_service[service]
        base = window_stats(sample, lo, windows.baseline_end_ns, end_inclusive=False)
        post = window_stats(sample, windows.post_start_ns, hi, end_inclusive=True)
        ev = assess_service(base, post, config=cfg, seed=used_seed)
        if has_latency_onset(ev):
            onset = estimate_onset(sample, base, windows, config=cfg, seed=used_seed)
            ev = ev.model_copy(update={"onset": onset})
        evidence[service] = ev

    triage: TriageResult | None = score_origins(evidence, graph, deployment)

    blast: BlastRadius | None = None
    relaxed_evidence: dict[str, ServiceEvidence] = {}
    if triage is not None:
        origin = triage.probable_origin or triage.candidates[0].service
        reachable = set(graph.bfs_upstream(origin)) | set(graph.bfs_downstream(origin))
        for service in sorted(reachable):
            strict = evidence[service].status
            # REGRESSED is already affected; INSUFFICIENT_DATA stays so (same min_requests).
            if strict in (ServiceStatus.REGRESSED, ServiceStatus.INSUFFICIENT_DATA):
                continue
            sample = per_service[service]
            base = window_stats(sample, lo, windows.baseline_end_ns, end_inclusive=False)
            post = window_stats(sample, windows.post_start_ns, hi, end_inclusive=True)
            relaxed_evidence[service] = assess_service(
                base, post, config=relaxed_cfg, seed=used_seed
            )
        blast = compute_blast_radius(
            graph,
            origin,
            {s: ev.status for s, ev in evidence.items()},
            {s: ev.status for s, ev in relaxed_evidence.items()},
        )

    severity, severity_service = _overall_severity(evidence)
    return RegressionAnalysis(
        engine_version=ENGINE_VERSION,
        tenant_id=tenant,
        deployment=deployment,
        windows=windows,
        config=cfg,
        relaxed_config=relaxed_cfg,
        seed=used_seed,
        status=_overall_status(evidence, deployment.service),
        severity=severity,
        severity_service=severity_service,
        services=tuple(evidence[s] for s in services),
        relaxed_services=tuple(relaxed_evidence[s] for s in sorted(relaxed_evidence)),
        graph=graph.snapshot(),
        triage=triage,
        blast_radius=blast,
        input=InputSummary(
            spans_received=len(received),
            other_environment_spans_dropped=len(received) - len(kept),
            duplicate_spans_dropped=index.duplicates_dropped,
            spans_in_windows=sum(1 for s in index if lo <= s.start_ns <= hi),
        ),
    )
