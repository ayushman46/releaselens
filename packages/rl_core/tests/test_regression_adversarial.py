"""Adversarial review tests for the regression engine, self-time, graph, triage and blast radius.

Each test pins a failure mode found (or ruled out) while trying to break Track A:

* error-rate gates exactly at a threshold, zero / 100% baselines, tiny error counts where the
  z-test's normal approximation is anti-conservative (control-scenario false alarms);
* NaN / inf / constant / tiny latency samples;
* determinism when timestamps tie (exporters truncate to µs/ms) and spans arrive in another order;
* onset only for services whose p95 latency regressed (no "latency onset" for error-only changes);
* a deployed service that crashes must not make its caller the probable origin;
* throughput-only WARNING end to end; wording never claims causation; evidence JSON round-trips.
"""

from __future__ import annotations

import random
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import tracegen as tg
from hypothesis import given, settings
from hypothesis import strategies as st
from tracegen import make_span

from rl_core.blast_radius import Impact, compute_blast_radius
from rl_core.graph import ServiceGraph
from rl_core.regression.analysis import RegressionAnalysis, analyse_deployment
from rl_core.regression.detect import (
    BOOTSTRAP_MAX_ELEMENTS,
    bootstrap_chunk,
    error_rate_evidence,
    fisher_exact_greater,
    latency_evidence,
)
from rl_core.regression.evidence import (
    DeploymentRef,
    Gate,
    OnsetEvidence,
    SampleGate,
    ServiceEvidence,
    ServiceStatus,
    SeverityBand,
    WindowSummary,
    percentile_label,
)
from rl_core.regression.onset import bootstrap_quantile_interval
from rl_core.regression.windows import WindowConfig
from rl_core.selftime import compute_self_times, compute_service_self_times
from rl_core.telemetry.index import TraceIndex
from rl_core.telemetry.model import NS_PER_MS, SpanKind, datetime_to_ns
from rl_core.triage import Confidence, Factor, score_origins

CFG = WindowConfig()
R, H, W, X = (
    ServiceStatus.REGRESSED,
    ServiceStatus.HEALTHY,
    ServiceStatus.WARNING,
    ServiceStatus.INSUFFICIENT_DATA,
)
S, C, IN, P, CO = (
    SpanKind.SERVER,
    SpanKind.CLIENT,
    SpanKind.INTERNAL,
    SpanKind.PRODUCER,
    SpanKind.CONSUMER,
)
CAUSAL_WORDING = re.compile(r"root[ _-]?cause|\bcaus(e|ed|es|ing)\b")


def _gate(ev: object, name: str) -> bool:
    gates: tuple[Gate, ...] = ev.gates  # type: ignore[attr-defined]
    return next(g.passed for g in gates if g.name == name)


def _run(scenario: tg.Scenario, seed: int = 0) -> RegressionAnalysis:
    return analyse_deployment(tg.generate(scenario, seed), scenario.deployment(), seed=seed)


# ============================================================================ error-rate gates


@pytest.mark.parametrize(
    ("x1", "n1", "x2", "n2"),
    [
        (2, 200, 3, 200),  # 1.0% → 1.5%: +0.5 pp exactly, +50% exactly
        (20, 2000, 30, 2000),
        (4, 400, 6, 400),
        (10, 1000, 15, 1000),
    ],
)
def test_error_threshold_boundaries_are_inclusive(x1: int, n1: int, x2: int, n2: int) -> None:
    # In floating point these are 0.49999999999999994 pp and +0.4999999999999999 relative, and
    # used to fail the "≥ 0.5 pp" / "≥ +50%" gates the spec defines as inclusive.
    e = error_rate_evidence(x1, n1, x2, n2, metric="error_rate", config=CFG)
    assert _gate(e, "absolute_increase_pp")
    assert _gate(e, "relative_increase")
    assert e.abs_delta_pp == pytest.approx(0.5)
    assert e.rel_delta == pytest.approx(0.5)
    # one request fewer is below the threshold again
    below = error_rate_evidence(x1, n1, x2 - 1, n2, metric="error_rate", config=CFG)
    assert not _gate(below, "absolute_increase_pp")


def test_user_decimal_thresholds_are_compared_as_written() -> None:
    # 0.3 pp threshold, observed exactly 3/1000 = 0.3 pp
    cfg = CFG.model_copy(update={"error_abs_threshold_pp": 0.3})
    e = error_rate_evidence(0, 1000, 3, 1000, metric="error_rate", config=cfg)
    assert _gate(e, "absolute_increase_pp")


@pytest.mark.parametrize(
    "field", ["error_abs_threshold_pp", "error_rel_threshold", "latency_abs_threshold_ms"]
)
def test_infinite_thresholds_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        WindowConfig.model_validate({field: float("inf")})


def test_zero_and_full_baselines() -> None:
    nothing = error_rate_evidence(0, 500, 0, 500, metric="error_rate", config=CFG)
    assert (nothing.p_value, nothing.exact_p_value, nothing.rel_delta) == (1.0, 1.0, None)
    assert not nothing.regressed
    # 100% → 100%: no variance, no increase possible
    full = error_rate_evidence(500, 500, 500, 500, metric="error_rate", config=CFG)
    assert not full.regressed
    assert full.p_value == 1.0
    assert full.exact_p_value == pytest.approx(1.0)
    # 100% → 90% is an improvement
    better = error_rate_evidence(500, 500, 450, 500, metric="error_rate", config=CFG)
    assert not better.regressed
    assert better.abs_delta_pp == pytest.approx(-10.0)
    # 0% → 3% on a few hundred requests is a real regression under every gate
    jump = error_rate_evidence(0, 1800, 25, 780, metric="error_rate", config=CFG)
    assert jump.regressed
    assert jump.rel_delta is None


def test_exact_test_blocks_small_count_false_alarm() -> None:
    # orders in the control scenario, seed 54: 2/1837 baseline errors → 5/751 post errors.
    # The z-test's normal approximation says p = 0.0066 (< 0.01); the exact test says 0.025.
    e = error_rate_evidence(2, 1837, 5, 751, metric="error_rate", config=CFG)
    assert _gate(e, "z_test_p_value")
    assert _gate(e, "absolute_increase_pp")
    assert _gate(e, "relative_increase")
    assert not _gate(e, "fisher_exact_p_value")
    assert e.exact_p_value == pytest.approx(0.0247, abs=0.0005)
    assert not e.regressed


def test_exact_test_agrees_with_z_test_on_large_counts() -> None:
    e = error_rate_evidence(10, 2000, 80, 2000, metric="error_rate", config=CFG)
    assert e.regressed
    assert e.exact_p_value is not None
    assert e.exact_p_value < 1e-10


@pytest.mark.parametrize(
    ("args", "match"),
    [((1, 0, 1, 10), "positive"), ((11, 10, 1, 10), "within"), ((1, 10, -1, 10), "within")],
)
def test_exact_test_rejects_impossible_counts(args: tuple[int, int, int, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        fisher_exact_greater(*args)


# ============================================================================ latency samples


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_latencies_are_rejected(bad: float) -> None:
    good = np.full(300, 50.0)
    poisoned = good.copy()
    poisoned[7] = bad
    with pytest.raises(ValueError, match="NaN or infinite"):
        latency_evidence(poisoned, good, 0.95, metric="latency", config=CFG, seed=0)
    with pytest.raises(ValueError, match="NaN or infinite"):
        latency_evidence(good, poisoned, 0.95, metric="latency", config=CFG, seed=0)
    with pytest.raises(ValueError, match="NaN or infinite"):
        bootstrap_quantile_interval(poisoned, 0.95)


def test_constant_latencies_never_regress() -> None:
    same = latency_evidence(
        np.full(400, 80.0), np.full(400, 80.0), 0.95, metric="latency", config=CFG, seed=3
    )
    assert (same.ci_low_ms, same.ci_high_ms, same.abs_delta_ms) == (0.0, 0.0, 0.0)
    assert not same.regressed
    zeros = latency_evidence(
        np.zeros(400), np.zeros(400), 0.95, metric="self_time", config=CFG, seed=3
    )
    assert zeros.rel_delta is None
    assert not zeros.regressed


def test_constant_shift_is_detected_with_a_degenerate_interval() -> None:
    e = latency_evidence(
        np.full(400, 80.0), np.full(400, 120.0), 0.95, metric="latency", config=CFG, seed=3
    )
    assert (e.ci_low_ms, e.ci_high_ms) == (40.0, 40.0)
    assert e.regressed


@pytest.mark.parametrize("n", [1, 2, 3])
def test_tiny_samples_are_deterministic_and_bounded(n: int) -> None:
    base = np.arange(1, n + 1, dtype=np.float64) * 10
    post = base + 100
    a = latency_evidence(base, post, 0.95, metric="latency", config=CFG, seed=9)
    b = latency_evidence(base, post, 0.95, metric="latency", config=CFG, seed=9)
    assert a == b
    assert (a.n_baseline, a.n_post) == (n, n)
    # every resample of a +100 ms shifted copy differs by at least 100 - (max - min) ms
    assert a.ci_low_ms >= 100 - 10 * (n - 1)
    assert a.ci_low_ms <= a.ci_high_ms


def test_bootstrap_chunk_bounds_memory_and_keeps_small_samples_unchanged() -> None:
    assert bootstrap_chunk(1) == bootstrap_chunk(1800, 780) == bootstrap_chunk(10_000) == 200
    assert bootstrap_chunk(100_000, 43_000) == 20
    assert bootstrap_chunk(5_000_000) == 1
    for n in (1, 999, 10_001, 123_457, 3_000_000):
        assert bootstrap_chunk(n) * n <= max(BOOTSTRAP_MAX_ELEMENTS, n)


def test_percentile_labels_stay_distinct() -> None:
    assert [percentile_label(q) for q in (0.5, 0.95, 0.99)] == ["p50", "p95", "p99"]
    assert percentile_label(0.995) != percentile_label(0.999)
    assert percentile_label(0.999) == "p99.9"


# ============================================================================ self-time


def _by_span_id(result: dict[tuple[str, str], int]) -> dict[str, float]:
    return {sid: ns / NS_PER_MS for (_, sid), ns in result.items()}


def test_self_time_zero_duration_spans() -> None:
    spans = [
        make_span("e", None, "a", S, 50, 50),
        make_span("c", "e", "a", C, 50, 50),
        make_span("z", "e", "a", IN, 50, 50),
    ]
    assert set(compute_service_self_times(spans).values()) == {0}
    assert set(compute_self_times(spans).values()) == {0}
    # a zero-length CLIENT inside a real request subtracts nothing
    spans = [make_span("e", None, "a", S, 0, 10), make_span("c", "e", "a", C, 5, 5)]
    assert compute_service_self_times(spans)[("t1", "e")] == 10 * NS_PER_MS


def test_self_time_nested_and_overlapping_outbound_calls() -> None:
    # c3 is a CLIENT nested inside CLIENT c1 (e.g. a retrying HTTP client): already covered.
    spans = [
        make_span("e", None, "a", S, 0, 100),
        make_span("c1", "e", "a", C, 10, 50),
        make_span("c2", "e", "a", C, 30, 70),
        make_span("c3", "c1", "a", C, 15, 45),
    ]
    assert compute_service_self_times(spans)[("t1", "e")] == 40 * NS_PER_MS


def test_self_time_async_producer_and_consumer() -> None:
    # fire-and-forget: PRODUCER outlives the request, the consumer runs later in another service
    spans = [
        make_span("e", None, "a", S, 0, 100),
        make_span("p", "e", "a", P, 90, 300),
        make_span("q", "p", "b", CO, 400, 500),
    ]
    assert compute_service_self_times(spans)[("t1", "e")] == 90 * NS_PER_MS
    assert compute_self_times(spans)[("t1", "e")] == 90 * NS_PER_MS


def test_self_time_child_outside_parent_and_self_call() -> None:
    skewed = [make_span("e", None, "a", S, 100, 200), make_span("c", "e", "a", C, 0, 50)]
    assert compute_service_self_times(skewed)[("t1", "e")] == 100 * NS_PER_MS
    # a calls itself over HTTP: the CLIENT wait is subtracted from the outer request, the inner
    # SERVER span is a request of its own with its own self-time
    self_call = [
        make_span("e", None, "a", S, 0, 100),
        make_span("c", "e", "a", C, 10, 60),
        make_span("e2", "c", "a", S, 12, 58),
    ]
    got = _by_span_id(compute_service_self_times(self_call))
    assert got == {"e": 50.0, "e2": 46.0}


def test_self_time_remote_span_below_internal_chain() -> None:
    spans = [
        make_span("e", None, "a", S, 0, 100),
        make_span("i", "e", "a", IN, 5, 95),
        make_span("j", "i", "a", IN, 10, 90),
        make_span("b", "j", "b", S, 20, 70),  # context propagated without a CLIENT span
    ]
    assert compute_service_self_times(spans)[("t1", "e")] == 50 * NS_PER_MS


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_self_time_survives_arbitrary_parent_links(data: st.DataObject) -> None:
    """Parent ids drawn freely (cycles, dangling parents, self-service SERVER children)."""
    n = data.draw(st.integers(1, 8))
    ids = [f"s{i}" for i in range(n)]
    spans = []
    for sid in ids:
        others = [x for x in ids if x != sid] + ["missing"]
        parent = data.draw(st.one_of(st.none(), st.sampled_from(others)))
        lo = data.draw(st.integers(0, 50))
        hi = data.draw(st.integers(lo, 60))
        spans.append(
            make_span(
                sid,
                parent,
                data.draw(st.sampled_from(["a", "b"])),
                data.draw(st.sampled_from([S, C, IN, P, CO])),
                lo,
                hi,
            )
        )
    index = TraceIndex.build(spans)
    service_self = compute_service_self_times(index, entries=list(index))
    span_self = compute_self_times(index)
    for span in index:
        assert 0 <= service_self[span.key] <= span.duration_ns
        assert 0 <= span_self[span.key] <= span.duration_ns
        index.is_own_error(span)  # terminates
    ServiceGraph.from_spans(index)  # terminates


# ============================================================================ graph


def test_graph_rejects_corrupt_edge_rows() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ServiceGraph.from_edges([("a", "a", -1)])
    with pytest.raises(ValueError, match="edge rows"):
        ServiceGraph.from_edges([("a",)])  # type: ignore[list-item]


def test_disconnected_components_never_leak_into_traversals() -> None:
    g = ServiceGraph.from_edges(
        [("a", "b"), ("b", "a"), ("b", "c"), ("x", "y"), ("y", "z"), ("z", "x")], nodes=["lonely"]
    )
    assert g.bfs_downstream("a") == {"b": 1, "c": 2}
    assert g.bfs_upstream("c") == {"b": 1, "a": 2}
    assert g.bfs_downstream("x") == {"y": 1, "z": 2}
    assert g.bfs_downstream("lonely") == {}
    assert g.bfs_upstream("lonely") == {}
    radius = compute_blast_radius(g, "b", {"a": R, "c": H})
    assert set(radius.not_in_radius) == {"lonely", "x", "y", "z"}


_svc = st.sampled_from([f"s{i}" for i in range(6)])
_status = st.sampled_from([R, H, W, X])


@settings(max_examples=300, deadline=None)
@given(
    edges=st.lists(st.tuples(_svc, _svc), max_size=15),
    strict=st.dictionaries(_svc, _status),
    relaxed=st.dictionaries(_svc, _status),
    origin=_svc,
)
def test_blast_radius_partitions_graph_and_never_calls_unknown_healthy(
    edges: list[tuple[str, str]],
    strict: dict[str, ServiceStatus],
    relaxed: dict[str, ServiceStatus],
    origin: str,
) -> None:
    g = ServiceGraph.from_edges(edges, nodes=[origin])
    radius = compute_blast_radius(g, origin, strict, relaxed)
    placed = [n.service for n in radius.nodes]
    assert len(placed) == len(set(placed))
    assert sorted(placed + list(radius.not_in_radius)) == sorted(s for s in g.nodes if s != origin)
    for node in radius.nodes:
        s = strict.get(node.service, X)
        rel = relaxed.get(node.service)
        affected = R in (s, rel)
        assert (node.service in radius.affected) == affected
        if node.impact in (Impact.HEALTHY_UPSTREAM, Impact.HEALTHY_DOWNSTREAM):
            # never "healthy" without some assessment that says so
            assert s in (H, W) or rel in (H, W)


# ============================================================================ triage


T = datetime(2026, 10, 13, 14, 2, tzinfo=UTC)


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


def _ev(
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
    return ServiceEvidence(
        service=service,
        variant="strict",
        status=status,
        sample_gate=SampleGate(
            baseline_requests=1000, post_requests=500, min_requests=200, passed=status is not X
        ),
        baseline=_summary(),
        post=_summary(),
        onset=onset,
        self_time_regressed=self_reg,
        originates_errors=orig_err,
    )


def _dep(service: str) -> DeploymentRef:
    return DeploymentRef(service=service, version="v2", previous_version="v1", deployed_at=T)


DEMO = ServiceGraph.from_edges(
    [("gateway", "orders"), ("orders", "inventory"), ("orders", "payment"), ("payment", "fraud")]
)


def test_unassessed_downstream_is_not_called_healthy() -> None:
    evidence = {
        "gateway": _ev("gateway", R),
        "orders": _ev("orders", R, orig_err=True),
        "inventory": _ev("inventory", H),
        "payment": _ev("payment", X),
        "fraud": _ev("fraud", X),
    }
    result = score_origins(evidence, DEMO, _dep("payment"))
    assert result is not None
    orders = next(c for c in result.candidates if c.service == "orders")
    factor = next(f for f in orders.factors if f.factor is Factor.DOWNSTREAM_HEALTHY)
    assert not factor.applies
    assert factor.reason == (
        "inventory (downstream) healthy; payment and fraud (downstream) could not be assessed "
        "(insufficient data), so a downstream origin cannot be ruled out"
    )
    assert orders.score == 2 + 1  # originates errors + gateway degraded
    assert result.confidence is Confidence.LOW
    assert result.probable_origin is None


def test_unassessed_upstream_is_reported_as_such() -> None:
    evidence = {
        "gateway": _ev("gateway", X),
        "orders": _ev("orders", H),
        "inventory": _ev("inventory", H),
        "payment": _ev("payment", R, self_reg=True),
        "fraud": _ev("fraud", H),
    }
    result = score_origins(evidence, DEMO, _dep("payment"))
    assert result is not None
    factor = next(f for f in result.candidates[0].factors if f.factor is Factor.UPSTREAM_DEGRADED)
    assert not factor.applies
    assert "gateway could not be assessed" in factor.reason


def test_tie_at_medium_level_names_no_origin() -> None:
    g = ServiceGraph.from_edges([], nodes=["a", "b", "c"])
    evidence = {"a": _ev("a", R, self_reg=True), "b": _ev("b", R, self_reg=True), "c": _ev("c", H)}
    result = score_origins(evidence, g, _dep("zzz"))  # 3 + 2 each
    assert result is not None
    assert (result.top_score, result.margin) == (5, 0)
    assert result.confidence is Confidence.LOW
    assert result.probable_origin is None
    assert result.shortlist == ("a", "b")
    # a one-point lead is enough for MEDIUM
    evidence["a"] = _ev("a", R, self_reg=True, onset_min=0)
    lead = score_origins(evidence, g, _dep("zzz"))
    assert lead is not None
    assert (lead.confidence, lead.probable_origin, lead.margin) == (Confidence.MEDIUM, "a", 1)


@settings(max_examples=300, deadline=None)
@given(
    edges=st.lists(st.tuples(_svc, _svc), max_size=12),
    statuses=st.dictionaries(_svc, _status, min_size=1),
    flags=st.dictionaries(
        _svc, st.tuples(st.booleans(), st.booleans(), st.none() | st.integers(0, 5))
    ),
    deployed=_svc,
)
def test_triage_invariants(
    edges: list[tuple[str, str]],
    statuses: dict[str, ServiceStatus],
    flags: dict[str, tuple[bool, bool, int | None]],
    deployed: str,
) -> None:
    g = ServiceGraph.from_edges(edges, nodes=list(statuses))
    evidence = {}
    for s, status in statuses.items():
        self_reg, orig_err, onset = flags.get(s, (False, False, None))
        evidence[s] = _ev(s, status, self_reg=self_reg, orig_err=orig_err, onset_min=onset)
    result = score_origins(evidence, g, _dep(deployed))
    regressed = {s for s, st_ in statuses.items() if st_ is R}
    if not regressed:
        assert result is None
        return
    assert result is not None
    assert {c.service for c in result.candidates} == regressed
    scores = [c.score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)
    assert (result.probable_origin is None) == (result.confidence is Confidence.LOW)
    if result.probable_origin is not None:
        # a named probable origin is always the sole leader
        assert result.probable_origin == result.candidates[0].service
        assert scores.count(result.top_score) == 1
    assert set(result.shortlist) >= {c.service for c in result.candidates if c.score == scores[0]}
    for cand in result.candidates:
        assert cand.score == sum(f.points for f in cand.factors)
        down = next(f for f in cand.factors if f.factor is Factor.DOWNSTREAM_HEALTHY)
        if down.applies:
            for d in g.bfs_downstream(cand.service):
                assert d in evidence  # assessed...
                assert evidence[d].status in (H, W)  # ...and not regressed
    assert not CAUSAL_WORDING.search(result.model_dump_json().lower())


# ============================================================================ end to end


def test_throughput_only_warning_end_to_end() -> None:
    scenario = tg.control()
    spans = tg.generate(scenario, 0)
    t = datetime_to_ns(tg.T_DEPLOY)
    after = sorted({s.trace_id for s in spans if s.parent_span_id is None and s.start_ns >= t})
    dropped = set(after[::2])  # half of the post-deploy traffic never arrives
    a = analyse_deployment([s for s in spans if s.trace_id not in dropped], scenario.deployment())
    assert a.status is W
    assert {ev.status for ev in a.services} == {W}
    for ev in a.services:
        assert ev.throughput is not None
        assert ev.throughput.dropped
        assert ev.regressed_metrics == ()
        assert ev.severity is not None
        assert ev.severity.score == 0.0
    assert a.severity is not None
    assert a.severity.band is SeverityBand.HEALTHY  # status WARNING, band HEALTHY: distinct
    assert a.triage is None  # nothing regressed, so nothing to originate
    assert a.blast_radius is None
    assert RegressionAnalysis.model_validate_json(a.model_dump_json()) == a


def test_error_only_regression_has_no_latency_onset() -> None:
    a = _run(tg.payment_errors_only())
    regressed = {ev.service: ev for ev in a.services if ev.status is R}
    assert set(regressed) == {"payment", "orders", "gateway"}
    for ev in regressed.values():
        assert ev.regressed_metrics == ("error_rate",)
        assert ev.onset is None
    assert a.triage is not None
    assert (a.triage.probable_origin, a.triage.confidence) == ("payment", Confidence.HIGH)
    for cand in a.triage.candidates:
        assert all(f.factor is not Factor.EARLIEST_ONSET for f in cand.factors if f.applies)
    assert not any("latency onset" in line for c in a.triage.candidates for line in c.reasoning)


def test_crashed_deployed_service_does_not_make_its_caller_the_origin() -> None:
    a = _run(tg.payment_crash())
    status = {ev.service: ev.status for ev in a.services}
    assert status == {"gateway": R, "orders": R, "inventory": H, "payment": X, "fraud": X}
    assert a.status is R
    # orders' refused calls are its own errors (no instrumented callee answered)...
    assert a.service("orders").originates_errors
    assert a.triage is not None
    orders = next(c for c in a.triage.candidates if c.service == "orders")
    down = next(f for f in orders.factors if f.factor is Factor.DOWNSTREAM_HEALTHY)
    # ...but payment, which stopped reporting, is never presented as healthy
    assert not down.applies
    assert "payment and fraud (downstream) could not be assessed" in down.reason
    assert a.triage.confidence is Confidence.LOW
    assert a.triage.probable_origin is None
    assert a.blast_radius is not None
    impacts = {n.service: n.impact for n in a.blast_radius.nodes}
    assert impacts["payment"] is Impact.UNKNOWN_DOWNSTREAM
    assert impacts["fraud"] is Impact.UNKNOWN_DOWNSTREAM
    assert RegressionAnalysis.model_validate_json(a.model_dump_json()) == a


# Control seeds (0..199) on which the pre-review engine raised an error-rate false alarm that came
# from the z-test's normal approximation on a handful of errors (see
# test_exact_test_blocks_small_count_false_alarm). Seeds 142 and 191 still alarm: 0 → 4 errors
# has exact p ≈ 0.008 < 0.01, a genuine chance event at the configured significance level.
FIXED_FALSE_ALARM_SEEDS = (54, 94, 96, 115)


@pytest.mark.parametrize("seed", FIXED_FALSE_ALARM_SEEDS)
def test_control_seeds_that_used_to_false_alarm(seed: int) -> None:
    a = _run(tg.control(), seed)
    assert a.status is H
    assert {ev.status for ev in a.services} == {H}
    assert a.triage is None


@pytest.mark.parametrize("seed", [142, 191])
def test_remaining_control_alarms_are_exact_test_significant(seed: int) -> None:
    """Pins the residual false-alarm mechanism so a change to it is noticed, not hidden."""
    a = _run(tg.control(), seed)
    alarms = [ev for ev in a.services if ev.status is not H]
    assert len(alarms) == 1
    (ev,) = alarms
    assert ev.regressed_metrics == ("error_rate",)
    assert ev.error_rate is not None
    assert (ev.error_rate.baseline_errors, ev.error_rate.post_errors) == (0, 4)
    assert ev.error_rate.exact_p_value is not None
    assert ev.error_rate.exact_p_value < CFG.error_p_threshold


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fraud_slowdown_never_blames_payment(seed: int) -> None:
    for scenario in (tg.fraud_regression(), tg.fraud_regression_payment_deployed()):
        a = _run(scenario, seed)
        assert a.triage is not None
        assert a.triage.probable_origin == "fraud"
        assert a.triage.candidates[0].service == "fraud"
        pay = a.service("payment")
        assert not pay.self_time_regressed
        assert not pay.originates_errors


@pytest.mark.parametrize(
    "scenario",
    [
        tg.payment_regression(),
        tg.fraud_regression(),
        tg.fraud_regression_payment_deployed(),
        tg.payment_crash(),
        tg.payment_errors_only(),
        tg.control(),
        tg.low_traffic(),
    ],
    ids=lambda s: s.name,
)
def test_wording_and_json_round_trip(scenario: tg.Scenario) -> None:
    first = _run(scenario, 5)
    text = first.model_dump_json()
    assert not CAUSAL_WORDING.search(text.lower())
    assert "NaN" not in text
    assert "Infinity" not in text
    assert RegressionAnalysis.model_validate_json(text) == first


@pytest.mark.parametrize(
    "scenario", [tg.payment_crash(), tg.fraud_regression()], ids=lambda s: s.name
)
def test_same_seed_gives_identical_json(scenario: tg.Scenario) -> None:
    assert _run(scenario, 4).model_dump_json() == _run(scenario, 4).model_dump_json()


def test_tied_timestamps_do_not_make_results_depend_on_input_order() -> None:
    """Exporters often truncate to µs/ms; tied start times must not change any interval."""
    scenario = tg.payment_regression()
    tick = 100 * NS_PER_MS  # coarse ticks: many requests per service share a start time
    spans = [
        replace(
            s, start_ns=s.start_ns // tick * tick, end_ns=max(s.start_ns // tick * tick, s.end_ns)
        )
        for s in tg.generate(scenario, 0)
    ]
    first = analyse_deployment(spans, scenario.deployment(), seed=0)
    random.Random(7).shuffle(spans)
    again = analyse_deployment(spans, scenario.deployment(), seed=0)
    assert again.model_dump_json() == first.model_dump_json()
