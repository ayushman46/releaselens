"""Probable-origin scoring (spec M9).

Candidates are the services whose status is **REGRESSED**. WARNING (throughput-only) services
are not candidates: nothing regressed there for a change to originate. Each candidate is scored
with the fixed weights of the spec table:

=====  ==========================  ==========================================================
 pts   factor                      definition used here
=====  ==========================  ==========================================================
 +3    ``deployed``                the candidate is the deployment event's service
 +3    ``self_time_regressed``     its service self-time gates passed at p95 or p99
                                   (:attr:`ServiceEvidence.self_time_regressed`)
 +2    ``originates_errors``       its error-rate gate passed **and** its own-error rate
                                   (failures not explained by a failing callee) passed the
                                   same gates (:attr:`ServiceEvidence.originates_errors`)
 +2    ``downstream_healthy``      every service reachable downstream (transitively) was assessed
                                   and is not REGRESSED (WARNING counts as healthy: its latency
                                   and error gates passed); an INSUFFICIENT_DATA downstream
                                   service blocks the factor; vacuously true with no downstream
 +1    ``upstream_degraded``       at least one service reachable upstream (transitively) is
                                   REGRESSED or WARNING — consistent with propagation
 +1    ``earliest_onset``          its estimated onset equals the earliest onset among
                                   candidates (all tied candidates score); candidates without
                                   an onset estimate never score
 -3    ``downstream_self_regressed``  a service reachable downstream is REGRESSED *and* has its
                                   own self-time regression (the change is further down)
=====  ==========================  ==========================================================

**Ranking:** score descending, then earlier onset (no onset ranks last), then service name.

**Confidence:** HIGH if the top score is ≥ 8 and its margin over the second candidate is ≥ 3 (a
single candidate has no competitor: the margin rule is satisfied); MEDIUM if the top score is
≥ 5 and no other candidate shares it; otherwise LOW. With HIGH/MEDIUM the top candidate is the
*probable origin*; with LOW there is none and the shortlist holds up to three candidates.
Candidates tied with the top score are always on the shortlist.

Why a tie is LOW: with equal scores the ranking falls through to the onset estimate (noisy) and
then the service *name*. Naming one of two equally supported services "the probable origin"
because it sorts first alphabetically would be a claim the evidence does not make.

**Why INSUFFICIENT_DATA downstream blocks ``downstream_healthy``:** the factor asserts that the
change is not further down. A dependency that stopped reporting — the deployed service crashing
after the release is the extreme case — cannot support that assertion. Counting it as healthy
named the crashed service's *caller* as the probable origin (MEDIUM confidence in the
``payment_crash`` test scenario: the caller's refused calls are its own errors, and its
dependencies "looked" healthy); now that scenario has no single probable origin (LOW).

Wording: this module only ever claims a **probable origin**, never a cause.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from rl_core.graph import ServiceGraph
from rl_core.regression.detect import TAIL_PERCENTILE
from rl_core.regression.evidence import (
    DeploymentRef,
    LatencyEvidence,
    ServiceEvidence,
    ServiceStatus,
    percentile_label,
)


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Factor(StrEnum):
    DEPLOYED = "deployed"
    SELF_TIME_REGRESSED = "self_time_regressed"
    ORIGINATES_ERRORS = "originates_errors"
    DOWNSTREAM_HEALTHY = "downstream_healthy"
    UPSTREAM_DEGRADED = "upstream_degraded"
    EARLIEST_ONSET = "earliest_onset"
    DOWNSTREAM_SELF_REGRESSED = "downstream_self_regressed"


WEIGHTS: Final[Mapping[Factor, int]] = {
    Factor.DEPLOYED: 3,
    Factor.SELF_TIME_REGRESSED: 3,
    Factor.ORIGINATES_ERRORS: 2,
    Factor.DOWNSTREAM_HEALTHY: 2,
    Factor.UPSTREAM_DEGRADED: 1,
    Factor.EARLIEST_ONSET: 1,
    Factor.DOWNSTREAM_SELF_REGRESSED: -3,
}

HIGH_MIN_SCORE: Final = 8
HIGH_MIN_MARGIN: Final = 3
MEDIUM_MIN_SCORE: Final = 5
LOW_SHORTLIST_SIZE: Final = 3


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FactorResult(_Model):
    factor: Factor
    applies: bool
    points: int  # the factor's weight when it applies, else 0
    reason: str


class OriginCandidate(_Model):
    service: str
    score: int
    onset_at: datetime | None
    factors: tuple[FactorResult, ...]
    reasoning: tuple[str, ...]  # human-readable lines for the factors that apply


class TriageResult(_Model):
    candidates: tuple[OriginCandidate, ...]  # ranked
    confidence: Confidence
    probable_origin: str | None  # None when confidence is LOW
    shortlist: tuple[str, ...]
    top_score: int
    margin: int | None  # top - second; None with a single candidate
    weights: dict[str, int]
    summary: str


# ----------------------------------------------------------------------------- text helpers
def _ms(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 10 else f"{value:.1f}"


def _pct(rel: float | None) -> str:
    return "n/a" if rel is None else f"{rel * 100:+.0f}%"


def _names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _by_distance(depths: Mapping[str, int]) -> list[str]:
    return sorted(depths, key=lambda name: (depths[name], name))


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M UTC")


def _versions(deployment: DeploymentRef) -> str:
    if deployment.previous_version and deployment.version:
        return f" ({deployment.previous_version} → {deployment.version})"
    if deployment.version:
        return f" (→ {deployment.version})"
    return ""


def _tail_self_time(ev: ServiceEvidence) -> LatencyEvidence | None:
    """The self-time evidence to quote: the first regressed tail percentile, else p95/first tail."""
    tail = [e for e in ev.self_time if e.percentile >= TAIL_PERCENTILE]
    regressed = [e for e in tail if e.regressed]
    if regressed:
        return regressed[0]
    return tail[0] if tail else None


def _self_time_line(service: str, e: LatencyEvidence) -> str:
    pq = percentile_label(e.percentile)
    return (
        f"{service} self-time {pq} {_ms(e.baseline_ms)} → {_ms(e.post_ms)} ms ({_pct(e.rel_delta)})"
    )


# ----------------------------------------------------------------------------- scoring
def _status(evidence: Mapping[str, ServiceEvidence], service: str) -> ServiceStatus:
    ev = evidence.get(service)
    return ServiceStatus.INSUFFICIENT_DATA if ev is None else ev.status


def _factor(factor: Factor, applies: bool, reason: str) -> FactorResult:
    return FactorResult(
        factor=factor, applies=applies, points=WEIGHTS[factor] if applies else 0, reason=reason
    )


def _score_candidate(
    service: str,
    evidence: Mapping[str, ServiceEvidence],
    graph: ServiceGraph,
    deployment: DeploymentRef,
    earliest_onset: datetime | None,
    tied_earliest: list[str],
) -> OriginCandidate:
    ev = evidence[service]
    # Nearest first (hop count, then name): "orders and gateway", not "gateway and orders".
    downstream = _by_distance(graph.bfs_downstream(service)) if service in graph else []
    upstream = _by_distance(graph.bfs_upstream(service)) if service in graph else []
    factors: list[FactorResult] = []

    # +3 deployed
    deployed = service == deployment.service
    factors.append(
        _factor(
            Factor.DEPLOYED,
            deployed,
            f"{service} was deployed at {_hhmm(deployment.deployed_at)}{_versions(deployment)}"
            if deployed
            else f"{service} was not deployed in this window ({deployment.service} was)",
        )
    )

    # +3 own self-time regressed
    st = _tail_self_time(ev)
    if ev.self_time_regressed and st is not None:
        st_reason = _self_time_line(service, st)
    elif st is not None:
        st_reason = f"{_self_time_line(service, st)}: no own self-time regression at p95/p99"
    else:
        st_reason = f"{service} self-time not assessed"
    factors.append(_factor(Factor.SELF_TIME_REGRESSED, ev.self_time_regressed, st_reason))

    # +2 originates errors
    own = ev.own_error_rate
    if ev.originates_errors and own is not None:
        err_reason = (
            f"{service} originates errors: own error rate {own.baseline_rate * 100:.2f}% → "
            f"{own.post_rate * 100:.2f}% ({own.abs_delta_pp:+.2f} pp, p = {own.p_value:.2g})"
        )
    elif ev.error_rate is not None and ev.error_rate.regressed:
        err_reason = (
            f"{service} error rate rose but its own-error rate did not: the extra failed "
            "requests had a failing callee"
        )
    else:
        err_reason = f"{service} error rate did not regress"
    factors.append(_factor(Factor.ORIGINATES_ERRORS, ev.originates_errors, err_reason))

    # +2 all downstream healthy: assessed and not REGRESSED
    regressed_down = [d for d in downstream if _status(evidence, d) is ServiceStatus.REGRESSED]
    unknown_down = [
        d for d in downstream if _status(evidence, d) is ServiceStatus.INSUFFICIENT_DATA
    ]
    healthy_down = [d for d in downstream if d not in regressed_down and d not in unknown_down]
    if not downstream:
        down_reason = f"{service} has no downstream dependencies in the traces"
    elif regressed_down:
        down_reason = f"{_names(regressed_down)} (downstream) also regressed"
    elif unknown_down:
        # A dependency without enough telemetry may be exactly where the change is (e.g. the
        # deployed service crashed and stopped reporting), so it is never presented as healthy.
        healthy = f"{_names(healthy_down)} (downstream) healthy; " if healthy_down else ""
        down_reason = (
            f"{healthy}{_names(unknown_down)} (downstream) could not be assessed "
            "(insufficient data), so a downstream origin cannot be ruled out"
        )
    else:
        down_reason = f"{_names(downstream)} (downstream) healthy"
    down_ok = not regressed_down and not unknown_down
    factors.append(_factor(Factor.DOWNSTREAM_HEALTHY, down_ok, down_reason))

    # +1 upstream callers degraded
    degraded_up = [
        u
        for u in upstream
        if _status(evidence, u) in (ServiceStatus.REGRESSED, ServiceStatus.WARNING)
    ]
    unknown_up = [u for u in upstream if _status(evidence, u) is ServiceStatus.INSUFFICIENT_DATA]
    if degraded_up:
        up_reason = (
            f"{_names(degraded_up)} (upstream) degraded, consistent with propagation from {service}"
        )
    elif unknown_up:
        up_reason = (
            f"no assessed upstream caller of {service} degraded; {_names(unknown_up)} could not be "
            "assessed (insufficient data)"
        )
    elif upstream:
        up_reason = f"no upstream caller of {service} degraded"
    else:
        up_reason = f"{service} has no upstream callers in the traces"
    factors.append(_factor(Factor.UPSTREAM_DEGRADED, bool(degraded_up), up_reason))

    # +1 earliest onset
    onset = ev.onset.onset_at if ev.onset is not None else None
    is_earliest = onset is not None and onset == earliest_onset
    if is_earliest and onset is not None:
        others = [s for s in tied_earliest if s != service]
        tie = f" (tied with {_names(others)})" if others else ""
        onset_reason = f"earliest latency onset among regressed services: {_hhmm(onset)}{tie}"
    elif onset is not None and earliest_onset is not None:
        onset_reason = (
            f"{service} latency onset {_hhmm(onset)} is after the earliest "
            f"({_hhmm(earliest_onset)})"
        )
    else:
        onset_reason = f"no latency onset estimated for {service}"
    factors.append(_factor(Factor.EARLIEST_ONSET, is_earliest, onset_reason))

    # -3 a downstream dependency regressed with its own self-time regression
    self_down = [d for d in regressed_down if evidence[d].self_time_regressed]
    if self_down:
        parts = []
        for d in self_down:
            dst = _tail_self_time(evidence[d])
            parts.append(_self_time_line(d, dst) if dst is not None else d)
        sd_reason = (
            f"{_names(self_down)} (downstream) also regressed with its own self-time "
            f"regression, so the probable origin is further downstream: {'; '.join(parts)}"
        )
    else:
        sd_reason = "no downstream service regressed with its own self-time regression"
    factors.append(_factor(Factor.DOWNSTREAM_SELF_REGRESSED, bool(self_down), sd_reason))

    score = sum(f.points for f in factors)
    # Positive evidence first, then the penalty, each in table order.
    reasoning = tuple(f.reason for f in factors if f.applies and f.points > 0) + tuple(
        f.reason for f in factors if f.applies and f.points < 0
    )
    return OriginCandidate(
        service=service, score=score, onset_at=onset, factors=tuple(factors), reasoning=reasoning
    )


def _rank_key(c: OriginCandidate) -> tuple[int, int, float, str]:
    # Earlier onset first; candidates without an onset after every candidate with one.
    has_onset = 0 if c.onset_at is not None else 1
    onset_ts = c.onset_at.timestamp() if c.onset_at is not None else 0.0
    return (-c.score, has_onset, onset_ts, c.service)


def score_origins(
    evidence: Mapping[str, ServiceEvidence],
    graph: ServiceGraph,
    deployment: DeploymentRef,
) -> TriageResult | None:
    """Score every REGRESSED service as a probable origin. ``None`` when nothing regressed."""
    regressed = sorted(s for s, ev in evidence.items() if ev.status is ServiceStatus.REGRESSED)
    if not regressed:
        return None

    onsets: dict[str, datetime] = {}
    for s in regressed:
        onset = evidence[s].onset
        if onset is not None and onset.onset_at is not None:
            onsets[s] = onset.onset_at
    earliest = min(onsets.values()) if onsets else None
    tied = sorted(s for s, t in onsets.items() if t == earliest)

    candidates = sorted(
        (_score_candidate(s, evidence, graph, deployment, earliest, tied) for s in regressed),
        key=_rank_key,
    )
    top = candidates[0]
    margin = top.score - candidates[1].score if len(candidates) > 1 else None
    sole_leader = margin is None or margin > 0

    if top.score >= HIGH_MIN_SCORE and (margin is None or margin >= HIGH_MIN_MARGIN):
        confidence = Confidence.HIGH
    elif top.score >= MEDIUM_MIN_SCORE and sole_leader:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    tied_top = [c.service for c in candidates if c.score == top.score]
    if confidence is Confidence.LOW:
        shortlist = [c.service for c in candidates[:LOW_SHORTLIST_SIZE]]
        shortlist += [s for s in tied_top if s not in shortlist]
        probable: str | None = None
        summary = "no single probable origin (LOW confidence); candidates: " + ", ".join(
            f"{c.service} ({c.score})" for c in candidates if c.service in shortlist
        )
    else:
        shortlist = tied_top
        probable = top.service
        runner_up = (
            f"; next: {candidates[1].service} ({candidates[1].score})"
            if len(candidates) > 1
            else ""
        )
        summary = (
            f"{top.service} is the probable origin (score {top.score}, "
            f"{confidence.value} confidence{runner_up})"
        )
    return TriageResult(
        candidates=tuple(candidates),
        confidence=confidence,
        probable_origin=probable,
        shortlist=tuple(shortlist),
        top_score=top.score,
        margin=margin,
        weights={f.value: w for f, w in WEIGHTS.items()},
        summary=summary,
    )
