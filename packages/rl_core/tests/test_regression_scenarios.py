"""End-to-end ``analyse_deployment`` on deterministic synthetic traces of the demo topology.

k6 → gateway → orders → {inventory, payment → fraud}; see ``tracegen.py``.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import replace
from datetime import timedelta

import pytest
import tracegen as tg

from rl_core.blast_radius import Impact
from rl_core.regression.analysis import ENGINE_VERSION, RegressionAnalysis, analyse_deployment
from rl_core.regression.evidence import ServiceStatus, SeverityBand
from rl_core.triage import Confidence, Factor

R, H, X = ServiceStatus.REGRESSED, ServiceStatus.HEALTHY, ServiceStatus.INSUFFICIENT_DATA


def _run(scenario: tg.Scenario, seed: int = 0, **kwargs: object) -> RegressionAnalysis:
    spans = tg.generate(scenario, seed)
    return analyse_deployment(spans, scenario.deployment(), seed=seed, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def payment() -> RegressionAnalysis:
    return _run(tg.payment_regression())


@pytest.fixture(scope="module")
def fraud() -> RegressionAnalysis:
    return _run(tg.fraud_regression())


def _impacts(a: RegressionAnalysis) -> dict[str, Impact]:
    assert a.blast_radius is not None
    return {n.service: n.impact for n in a.blast_radius.nodes}


def _applied(a: RegressionAnalysis, service: str) -> set[Factor]:
    assert a.triage is not None
    cand = next(c for c in a.triage.candidates if c.service == service)
    return {f.factor for f in cand.factors if f.applies}


# ----------------------------------------------------------------------------- scenario 1


def test_scenario1_detection(payment: RegressionAnalysis) -> None:
    assert payment.status is R
    statuses = {ev.service: ev.status for ev in payment.services}
    assert statuses == {"gateway": R, "orders": R, "payment": R, "fraud": H, "inventory": H}

    pay = payment.service("payment")
    assert "latency_p95" in pay.regressed_metrics
    assert "error_rate" in pay.regressed_metrics
    assert pay.self_time_regressed
    assert pay.originates_errors
    assert pay.onset is not None
    assert pay.onset.onset_at == tg.T_DEPLOY  # change starts at T: first bucket

    # callers regressed in total latency and errors, but neither own time nor own errors
    for caller in ("orders", "gateway"):
        ev = payment.service(caller)
        assert "latency_p95" in ev.regressed_metrics
        assert not ev.self_time_regressed
        assert not ev.originates_errors

    assert payment.severity is not None
    assert payment.severity.band is SeverityBand.HIGH
    assert payment.severity_service in {"payment", "orders", "gateway"}


def test_scenario1_origin_is_payment_with_high_confidence(payment: RegressionAnalysis) -> None:
    triage = payment.triage
    assert triage is not None
    assert triage.probable_origin == "payment"
    assert triage.confidence is Confidence.HIGH
    assert triage.top_score == 12
    assert _applied(payment, "payment") == {
        Factor.DEPLOYED,
        Factor.SELF_TIME_REGRESSED,
        Factor.ORIGINATES_ERRORS,
        Factor.DOWNSTREAM_HEALTHY,
        Factor.UPSTREAM_DEGRADED,
        Factor.EARLIEST_ONSET,
    }
    reasoning = triage.candidates[0].reasoning
    assert reasoning[0] == "payment was deployed at 14:02 UTC (v2.17 → v2.18)"
    assert reasoning[1].startswith("payment self-time p95 ")
    assert "fraud (downstream) healthy" in reasoning


def test_scenario1_blast_radius(payment: RegressionAnalysis) -> None:
    assert _impacts(payment) == {
        "orders": Impact.AFFECTED_UPSTREAM,
        "gateway": Impact.AFFECTED_UPSTREAM,
        "fraud": Impact.HEALTHY_DOWNSTREAM,  # reachable, but its own telemetry is fine
    }
    assert payment.blast_radius is not None
    assert payment.blast_radius.not_in_radius == ("inventory",)
    # fraud was re-checked with the relaxed detector and still did not regress
    relaxed = {ev.service: ev.status for ev in payment.relaxed_services}
    assert relaxed == {"fraud": H}


@pytest.mark.parametrize("seed", [1, 2])
def test_scenario1_other_seeds(seed: int) -> None:
    a = _run(tg.payment_regression(), seed)
    assert a.triage is not None
    assert (a.triage.probable_origin, a.triage.confidence) == ("payment", Confidence.HIGH)


# ----------------------------------------------------------------------------- scenario 2


def test_scenario2_origin_is_fraud_not_payment(fraud: RegressionAnalysis) -> None:
    triage = fraud.triage
    assert triage is not None
    assert triage.probable_origin == "fraud"
    assert triage.confidence is Confidence.HIGH

    pay = fraud.service("payment")
    assert pay.status is R  # its total latency rose (it waits for fraud)...
    assert "latency_p95" in pay.regressed_metrics
    assert not pay.self_time_regressed  # ...but its own time did not
    assert Factor.DOWNSTREAM_SELF_REGRESSED in _applied(fraud, "payment")
    assert fraud.service("fraud").self_time_regressed


def test_scenario2_blast_radius(fraud: RegressionAnalysis) -> None:
    assert _impacts(fraud) == {
        "payment": Impact.AFFECTED_UPSTREAM,
        "orders": Impact.AFFECTED_UPSTREAM,
        "gateway": Impact.AFFECTED_UPSTREAM,
    }
    assert fraud.blast_radius is not None
    assert fraud.blast_radius.not_in_radius == ("inventory",)


def test_scenario2b_payment_deployed_but_fraud_slowed() -> None:
    a = _run(tg.fraud_regression_payment_deployed())
    assert a.triage is not None
    assert a.triage.probable_origin == "fraud"
    # fraud: self-time 3 + downstream healthy 2 + upstream degraded 1 + earliest onset 1 = 7;
    # without the +3 "deployed" factor it cannot reach 8, so the spec rule gives MEDIUM.
    assert a.triage.top_score == 7
    assert a.triage.confidence is Confidence.MEDIUM


# ----------------------------------------------------------------------------- control


@pytest.mark.parametrize("seed", range(10))
def test_control_has_no_false_alarm(seed: int) -> None:
    a = _run(tg.control(), seed)
    assert a.status is H
    assert {ev.status for ev in a.services} == {H}
    assert a.triage is None
    assert a.blast_radius is None
    assert a.severity is not None
    assert a.severity.score == 0.0


# ----------------------------------------------------------------------------- low traffic


def test_low_traffic_is_insufficient_data() -> None:
    a = _run(tg.low_traffic())
    assert a.status is X
    assert {ev.status for ev in a.services} == {X}
    assert all(ev.sample_gate.baseline_requests < 200 for ev in a.services)
    assert a.severity is None
    assert a.triage is None
    assert a.blast_radius is None


# ----------------------------------------------------------------------------- partial windows


def test_as_of_partial_post_window() -> None:
    scenario = tg.control()
    spans = tg.generate(scenario, 0)
    early = analyse_deployment(
        spans, scenario.deployment(), as_of=tg.T_DEPLOY + timedelta(minutes=10)
    )
    assert early.windows.effective_post_end == tg.T_DEPLOY + timedelta(minutes=10)
    # throughput divided by the 8 observed minutes, not 13: no spurious drop
    assert early.status is H
    gateway = early.service("gateway")
    assert gateway.throughput is not None
    assert abs(gateway.throughput.rel_change or 0) < 0.15

    # at T+5 only 3 post minutes exist: 60 rpm x 3 < 200 requests → not yet assessable
    regressed = tg.generate(tg.payment_regression(), 0)
    dep = tg.payment_regression().deployment()
    at5 = analyse_deployment(regressed, dep, as_of=tg.T_DEPLOY + timedelta(minutes=5))
    assert at5.status is X
    at10 = analyse_deployment(regressed, dep, as_of=tg.T_DEPLOY + timedelta(minutes=10))
    assert at10.status is R


# ----------------------------------------------------------------------------- result contract


def test_result_is_json_serialisable_and_round_trips(payment: RegressionAnalysis) -> None:
    text = payment.model_dump_json()
    assert "NaN" not in text
    assert "Infinity" not in text
    data = json.loads(text)
    assert data["engine_version"] == ENGINE_VERSION == "regression-engine/1.0.0"
    assert data["windows"]["baseline_start"] == "2026-10-13T13:32:00Z"
    assert data["windows"]["post_end"] == "2026-10-13T14:17:00Z"
    assert data["config"]["min_requests"] == 200
    assert data["seed"] == 0
    assert {e["source"] + ">" + e["target"] for e in data["graph"]["edges"]} == {
        "gateway>orders",
        "orders>inventory",
        "orders>payment",
        "payment>fraud",
    }
    assert RegressionAnalysis.model_validate_json(text) == payment
    lowered = text.lower()
    assert "root cause" not in lowered
    assert "caused" not in lowered
    assert "probable origin" in lowered


def test_deterministic_and_input_order_independent(payment: RegressionAnalysis) -> None:
    spans = tg.generate(tg.payment_regression(), 0)
    random.Random(42).shuffle(spans)
    again = analyse_deployment(spans, tg.payment_regression().deployment(), seed=0)
    assert again.model_dump_json() == payment.model_dump_json()


def test_input_hygiene() -> None:
    scenario = tg.control(rate_per_minute=20)
    spans = tg.generate(scenario, 3)
    dep = scenario.deployment()
    clean = analyse_deployment(spans, dep)

    # at-least-once replays are dropped and change nothing
    replayed = analyse_deployment(spans + spans[:500], dep)
    assert replayed.input.duplicate_spans_dropped == 500
    assert replayed.services == clean.services

    # spans from another environment are dropped; spans without environment are kept
    staging = [replace(s, environment="staging", trace_id="s" + s.trace_id) for s in spans[:300]]
    unlabelled = [replace(s, environment=None) for s in spans]
    mixed = analyse_deployment(staging + unlabelled, dep)
    assert mixed.input.other_environment_spans_dropped == 300
    assert mixed.services == clean.services

    # one tenant per analysis
    other = [replace(spans[0], tenant_id="tenant-b", trace_id="x")]
    with pytest.raises(ValueError, match="tenants"):
        analyse_deployment(spans + other, dep)
    with pytest.raises(ValueError, match="different tenant"):
        analyse_deployment(spans, dep.model_copy(update={"tenant_id": "tenant-b"}))
    assert clean.tenant_id == "tenant-a"


def test_deployed_service_without_telemetry() -> None:
    scenario = tg.control()
    spans = tg.generate(scenario, 0)
    dep = scenario.deployment().model_copy(update={"service": "billing"})
    a = analyse_deployment(spans, dep)
    assert a.status is X  # the deployed service could not be assessed
    assert "billing" not in a.graph.nodes


# ----------------------------------------------------------------------------- performance


@pytest.mark.slow
def test_performance_sixty_thousand_spans() -> None:
    scenario = tg.payment_regression(rate_per_minute=95)
    spans = tg.generate(scenario, 0)
    assert 55_000 <= len(spans) <= 65_000
    start = time.perf_counter()
    a = analyse_deployment(spans, scenario.deployment())
    elapsed = time.perf_counter() - start
    assert a.triage is not None
    assert a.triage.probable_origin == "payment"
    # Sanity bound only (measured timings are reported separately); catches pathologies.
    assert elapsed < 20.0
