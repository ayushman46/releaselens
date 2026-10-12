"""Probable-origin scoring on hand-built evidence (every factor, confidence rule and tie-break)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from rl_core.graph import ServiceGraph
from rl_core.regression.evidence import (
    DeploymentRef,
    ErrorRateEvidence,
    LatencyEvidence,
    OnsetEvidence,
    SampleGate,
    ServiceEvidence,
    ServiceStatus,
    WindowSummary,
)
from rl_core.triage import WEIGHTS, Confidence, Factor, TriageResult, score_origins

T = datetime(2026, 10, 13, 14, 2, tzinfo=UTC)
R, H, W, X = (
    ServiceStatus.REGRESSED,
    ServiceStatus.HEALTHY,
    ServiceStatus.WARNING,
    ServiceStatus.INSUFFICIENT_DATA,
)

DEMO = ServiceGraph.from_edges(
    [("gateway", "orders"), ("orders", "inventory"), ("orders", "payment"), ("payment", "fraud")]
)


def _summary() -> WindowSummary:
    return WindowSummary(
        requests=1000,
        errors=0,
        error_rate=0.0,
        throughput_rpm=50.0,
        latency_p50_ms=None,
        latency_p95_ms=None,
        latency_p99_ms=None,
        self_time_p95_ms=None,
    )


def _self_time(q: float, base: float, post: float, regressed: bool) -> LatencyEvidence:
    return LatencyEvidence(
        metric="self_time",
        percentile=q,
        baseline_ms=base,
        post_ms=post,
        abs_delta_ms=post - base,
        rel_delta=(post - base) / base,
        ci_low_ms=post - base - 5,
        ci_high_ms=post - base + 5,
        ci_confidence=0.95,
        bootstrap_resamples=1000,
        bootstrap_seed=0,
        n_baseline=1000,
        n_post=500,
        gates=(),
        regressed=regressed,
    )


def _own_errors(base: float, post: float) -> ErrorRateEvidence:
    return ErrorRateEvidence(
        metric="own_error_rate",
        baseline_errors=round(base * 1000),
        baseline_requests=1000,
        post_errors=round(post * 500),
        post_requests=500,
        baseline_rate=base,
        post_rate=post,
        abs_delta_pp=(post - base) * 100,
        rel_delta=None,
        z=8.0,
        p_value=1e-15,
        gates=(),
        regressed=True,
    )


def ev(
    service: str,
    status: ServiceStatus,
    *,
    self_reg: bool = False,
    orig_err: bool = False,
    onset_min: int | None = None,
) -> ServiceEvidence:
    onset = None
    if onset_min is not None:
        onset = OnsetEvidence(
            onset_at=T + timedelta(minutes=onset_min),
            minutes_after_deploy=onset_min,
            baseline_p95_ms=100.0,
            baseline_p95_ci_high_ms=110.0,
            ci_confidence=0.95,
            bootstrap_resamples=1000,
            bootstrap_seed=0,
            bucket_seconds=60,
            min_samples_per_bucket=20,
            buckets=(),
        )
    post_self = 391.0 if self_reg else 113.0
    return ServiceEvidence(
        service=service,
        variant="strict",
        status=status,
        sample_gate=SampleGate(
            baseline_requests=1000, post_requests=500, min_requests=200, passed=status is not X
        ),
        baseline=_summary(),
        post=_summary(),
        self_time=(
            _self_time(0.5, 80.0, 82.0, False),
            _self_time(0.95, 112.0, post_self, self_reg),
            _self_time(0.99, 130.0, post_self + 20, self_reg),
        ),
        own_error_rate=_own_errors(0.0, 0.035) if orig_err else None,
        onset=onset,
        self_time_regressed=self_reg,
        originates_errors=orig_err,
    )


def _dep(service: str = "payment") -> DeploymentRef:
    return DeploymentRef(service=service, version="v2.18", previous_version="v2.17", deployed_at=T)


def _factors(result: TriageResult, service: str) -> set[Factor]:
    cand = next(c for c in result.candidates if c.service == service)
    return {f.factor for f in cand.factors if f.applies}


def _score(result: TriageResult, service: str) -> int:
    return next(c.score for c in result.candidates if c.service == service)


# ----------------------------------------------------------------------------- scenarios


def test_payment_origin_scenario_scores_every_factor() -> None:
    evidence = {
        "payment": ev("payment", R, self_reg=True, orig_err=True, onset_min=0),
        "orders": ev("orders", R, onset_min=0),
        "gateway": ev("gateway", R, onset_min=0),
        "fraud": ev("fraud", H),
        "inventory": ev("inventory", H),
    }
    result = score_origins(evidence, DEMO, _dep())
    assert result is not None
    assert [c.service for c in result.candidates] == ["payment", "orders", "gateway"]
    assert _factors(result, "payment") == {
        Factor.DEPLOYED,
        Factor.SELF_TIME_REGRESSED,
        Factor.ORIGINATES_ERRORS,
        Factor.DOWNSTREAM_HEALTHY,
        Factor.UPSTREAM_DEGRADED,
        Factor.EARLIEST_ONSET,
    }
    assert _score(result, "payment") == 3 + 3 + 2 + 2 + 1 + 1
    # orders: gateway upstream degraded +1, tied onset +1, payment below regressed with own
    # self-time -3; inventory is healthy but payment is not, so no +2.
    assert _factors(result, "orders") == {
        Factor.UPSTREAM_DEGRADED,
        Factor.EARLIEST_ONSET,
        Factor.DOWNSTREAM_SELF_REGRESSED,
    }
    assert _score(result, "orders") == -1
    assert _score(result, "gateway") == 1 - 3
    assert result.confidence is Confidence.HIGH
    assert result.probable_origin == "payment"
    assert result.shortlist == ("payment",)
    assert result.margin == 13
    assert "payment is the probable origin" in result.summary

    reasoning = next(c for c in result.candidates if c.service == "payment").reasoning
    assert reasoning[0] == "payment was deployed at 14:02 UTC (v2.17 → v2.18)"
    assert reasoning[1] == "payment self-time p95 112 → 391 ms (+249%)"
    assert "fraud (downstream) healthy" in reasoning
    assert "orders and gateway (upstream) degraded" in " ".join(reasoning)


def test_fraud_origin_when_payment_only_waits() -> None:
    evidence = {
        "fraud": ev("fraud", R, self_reg=True, onset_min=0),
        "payment": ev("payment", R, onset_min=0),  # total latency up, self-time not
        "orders": ev("orders", R, onset_min=0),
        "gateway": ev("gateway", R, onset_min=0),
        "inventory": ev("inventory", H),
    }
    deployed_fraud = score_origins(evidence, DEMO, _dep("fraud"))
    assert deployed_fraud is not None
    assert deployed_fraud.probable_origin == "fraud"
    assert _score(deployed_fraud, "fraud") == 3 + 3 + 2 + 1 + 1
    assert _score(deployed_fraud, "payment") == 1 + 1 - 3
    assert deployed_fraud.confidence is Confidence.HIGH

    # Same telemetry, but payment was the deployed service: fraud still ranks first, without the
    # +3 its score is 7 < 8, so confidence is MEDIUM (the spec weights are not tuned to force HIGH).
    deployed_payment = score_origins(evidence, DEMO, _dep("payment"))
    assert deployed_payment is not None
    assert deployed_payment.probable_origin == "fraud"
    assert _score(deployed_payment, "fraud") == 7
    assert _score(deployed_payment, "payment") == 3 + 1 + 1 - 3
    assert deployed_payment.confidence is Confidence.MEDIUM


# ----------------------------------------------------------------------------- definitions


def test_nothing_regressed_returns_none() -> None:
    evidence = {s: ev(s, H) for s in DEMO.nodes}
    evidence["orders"] = ev("orders", W)
    assert score_origins(evidence, DEMO, _dep()) is None


def test_downstream_warning_or_unknown_counts_as_healthy() -> None:
    evidence = {
        "payment": ev("payment", R),
        "fraud": ev("fraud", W),
        "orders": ev("orders", H),
        "gateway": ev("gateway", H),
        "inventory": ev("inventory", X),
    }
    result = score_origins(evidence, DEMO, _dep("inventory"))
    assert result is not None
    assert Factor.DOWNSTREAM_HEALTHY in _factors(result, "payment")
    evidence["fraud"] = ev("fraud", R)  # downstream regressed WITHOUT own self-time regression
    result = score_origins(evidence, DEMO, _dep("inventory"))
    assert result is not None
    assert Factor.DOWNSTREAM_HEALTHY not in _factors(result, "payment")
    assert Factor.DOWNSTREAM_SELF_REGRESSED not in _factors(result, "payment")


def test_upstream_warning_counts_as_degraded_and_is_transitive() -> None:
    evidence = {
        "payment": ev("payment", R),
        "orders": ev("orders", H),
        "gateway": ev("gateway", W),  # two hops up
        "fraud": ev("fraud", H),
        "inventory": ev("inventory", H),
    }
    result = score_origins(evidence, DEMO, _dep("inventory"))
    assert result is not None
    assert Factor.UPSTREAM_DEGRADED in _factors(result, "payment")


def test_earliest_onset_ties_and_missing_onsets() -> None:
    evidence = {
        "payment": ev("payment", R, onset_min=1),
        "orders": ev("orders", R, onset_min=1),
        "gateway": ev("gateway", R),  # error-only regression: no latency onset
        "fraud": ev("fraud", H),
        "inventory": ev("inventory", H),
    }
    result = score_origins(evidence, DEMO, _dep("inventory"))
    assert result is not None
    assert Factor.EARLIEST_ONSET in _factors(result, "payment")
    assert Factor.EARLIEST_ONSET in _factors(result, "orders")
    assert Factor.EARLIEST_ONSET not in _factors(result, "gateway")
    evidence["orders"] = ev("orders", R, onset_min=3)
    result = score_origins(evidence, DEMO, _dep("inventory"))
    assert result is not None
    assert Factor.EARLIEST_ONSET not in _factors(result, "orders")


def test_ranking_tie_breaks_onset_then_name() -> None:
    g = ServiceGraph.from_edges([], nodes=["a", "b", "c", "d"])
    evidence = {
        "a": ev("a", R),  # no onset → after b and c
        "b": ev("b", R, onset_min=4),
        "c": ev("c", R, onset_min=4),
        "d": ev("d", R, onset_min=2),
    }
    # isolated nodes: every candidate gets downstream_healthy +2; d also earliest onset +1
    result = score_origins(evidence, g, _dep("zzz"))
    assert result is not None
    assert [c.service for c in result.candidates] == ["d", "b", "c", "a"]


# ----------------------------------------------------------------------------- confidence


def test_single_candidate_with_eight_points_is_high() -> None:
    g = ServiceGraph.from_edges([("api", "db")])
    evidence = {"api": ev("api", R, self_reg=True, onset_min=0), "db": ev("db", H)}
    result = score_origins(evidence, g, _dep("api"))
    assert result is not None
    assert result.top_score == 3 + 3 + 2 + 1
    assert result.margin is None
    assert result.confidence is Confidence.HIGH


def test_high_needs_margin_of_three() -> None:
    g = ServiceGraph.from_edges([], nodes=["a", "b"])
    evidence = {
        "a": ev("a", R, self_reg=True, orig_err=True, onset_min=0),  # 3+2+2+1 = 8 (+3 deployed)
        "b": ev("b", R, self_reg=True, orig_err=True, onset_min=0),  # 8
    }
    result = score_origins(evidence, g, _dep("a"))
    assert result is not None
    assert (result.top_score, result.margin) == (11, 3)
    assert result.confidence is Confidence.HIGH
    evidence["b"] = ev("b", R, self_reg=True, orig_err=True, onset_min=0)
    evidence["a"] = ev("a", R, self_reg=True, orig_err=True, onset_min=0)
    tight = score_origins(evidence, g, _dep("zzz"))  # nobody deployed: 8 vs 8
    assert tight is not None
    assert tight.margin == 0
    # A tie at the top is never resolved into a single probable origin by the name tie-break.
    assert tight.confidence is Confidence.LOW
    assert tight.probable_origin is None
    assert [c.service for c in tight.candidates] == ["a", "b"]  # rank order (onset tie → name)
    assert tight.shortlist == ("a", "b")  # tied top scores are always shortlisted


def test_low_confidence_returns_several_candidates_and_no_origin() -> None:
    g = ServiceGraph.from_edges([("a", "b"), ("b", "c"), ("c", "d")])
    evidence = {s: ev(s, R) for s in "abcd"}  # chain, nothing distinctive
    result = score_origins(evidence, g, _dep("zzz"))
    assert result is not None
    assert result.top_score < 5
    assert result.confidence is Confidence.LOW
    assert result.probable_origin is None
    assert len(result.shortlist) == 3
    assert result.summary.startswith("no single probable origin (LOW confidence)")


def test_weights_are_the_spec_table() -> None:
    assert {f.value: w for f, w in WEIGHTS.items()} == {
        "deployed": 3,
        "self_time_regressed": 3,
        "originates_errors": 2,
        "downstream_healthy": 2,
        "upstream_degraded": 1,
        "earliest_onset": 1,
        "downstream_self_regressed": -3,
    }


@pytest.mark.parametrize("deployed", ["payment", "fraud", "zzz"])
def test_wording_never_claims_causation(deployed: str) -> None:
    evidence = {
        "payment": ev("payment", R, self_reg=True, orig_err=True, onset_min=0),
        "orders": ev("orders", R, onset_min=0),
        "gateway": ev("gateway", R, onset_min=1),
        "fraud": ev("fraud", R, self_reg=True, onset_min=0),
        "inventory": ev("inventory", X),
    }
    result = score_origins(evidence, DEMO, _dep(deployed))
    assert result is not None
    text = result.model_dump_json().lower()
    assert not re.search(r"root[ _-]?cause|caused", text)
