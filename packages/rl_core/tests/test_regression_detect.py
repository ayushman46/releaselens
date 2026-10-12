"""Gates, service status and severity v1 on hand-built samples."""

from __future__ import annotations

import json
import math

import numpy as np
import numpy.typing as npt
import pytest

from rl_core.regression.detect import (
    assess_service,
    band_for,
    error_rate_evidence,
    latency_evidence,
    severity_v1,
)
from rl_core.regression.evidence import Gate, ServiceEvidence, ServiceStatus, SeverityBand
from rl_core.regression.metrics import ServiceSamples, WindowStats
from rl_core.regression.windows import WindowConfig

CFG = WindowConfig()
MIN_NS = 60 * 1_000_000_000


def _stats(
    latency: npt.ArrayLike,
    *,
    minutes: float,
    self_time: npt.ArrayLike | None = None,
    errors: int = 0,
    own_errors: int | None = None,
    service: str = "svc",
) -> WindowStats:
    lat = np.asarray(latency, dtype=np.float64)
    n = lat.size
    err = np.zeros(n, dtype=np.bool_)
    err[:errors] = True
    own = np.zeros(n, dtype=np.bool_)
    own[: errors if own_errors is None else own_errors] = True
    end = round(minutes * MIN_NS)
    samples = ServiceSamples(
        service=service,
        start_ns=np.linspace(0, end - 1, n).astype(np.int64) if n else np.empty(0, np.int64),
        latency_ms=lat,
        self_time_ms=lat if self_time is None else np.asarray(self_time, dtype=np.float64),
        is_error=err,
        is_own_error=own,
    )
    return WindowStats(service, 0, end, False, samples)


def _gate(ev: object, name: str) -> bool:
    gates: tuple[Gate, ...] = ev.gates  # type: ignore[attr-defined]
    return next(g.passed for g in gates if g.name == name)


RNG = np.random.default_rng(11)
BASE = RNG.lognormal(math.log(100), 0.3, 3000)
POST = RNG.lognormal(math.log(100), 0.3, 1300)


# ----------------------------------------------------------------------------- latency gates


def test_clear_shift_passes_every_latency_gate() -> None:
    ev = assess_service(
        _stats(BASE, minutes=30), _stats(POST + 250, minutes=13), config=CFG, seed=0
    )
    assert ev.status is ServiceStatus.REGRESSED
    assert ev.regressed_metrics == ("latency_p50", "latency_p95", "latency_p99")
    for e in ev.latency:
        assert [g.name for g in e.gates] == [
            "relative_increase",
            "absolute_increase_ms",
            "bootstrap_ci_low_above_zero",
        ]
        assert all(g.passed for g in e.gates)
        assert e.ci_low_ms > 0
        assert e.n_baseline == 3000
        assert e.n_post == 1300
        assert e.bootstrap_resamples == 1000


def test_relative_gate_blocks_small_relative_change() -> None:
    # +60 ms on a ~1 s service: absolute and CI gates pass, +6% fails the 25% gate
    e = latency_evidence(BASE * 10, POST * 10 + 60, 0.5, metric="latency", config=CFG, seed=0)
    assert _gate(e, "absolute_increase_ms")
    assert _gate(e, "bootstrap_ci_low_above_zero")
    assert not _gate(e, "relative_increase")
    assert not e.regressed


def test_absolute_gate_blocks_small_absolute_change() -> None:
    # 10 ms → 15 ms: +50% but only +5 ms
    e = latency_evidence(BASE / 10, POST / 10 * 1.5, 0.5, metric="latency", config=CFG, seed=0)
    assert _gate(e, "relative_increase")
    assert _gate(e, "bootstrap_ci_low_above_zero")
    assert not _gate(e, "absolute_increase_ms")
    assert not e.regressed


def test_bootstrap_gate_blocks_a_noisy_tail() -> None:
    # p95 sits on the edge of a 5% tail of 1 s outliers: +490% / +855 ms on the point estimate,
    # but the bootstrap CI of the difference spans zero.
    base = [100.0] * 190 + [1000.0] * 10
    post = [100.0] * 188 + [1000.0] * 12
    e = latency_evidence(base, post, 0.95, metric="latency", config=CFG, seed=0)
    assert _gate(e, "relative_increase")
    assert _gate(e, "absolute_increase_ms")
    assert not _gate(e, "bootstrap_ci_low_above_zero")
    assert e.ci_low_ms <= 0 < e.ci_high_ms
    assert not e.regressed


def test_decrease_never_regresses() -> None:
    e = latency_evidence(BASE + 250, POST, 0.95, metric="latency", config=CFG, seed=0)
    assert e.abs_delta_ms < 0
    assert not any(g.passed for g in e.gates)


def test_zero_baseline_percentile_has_unbounded_relative_change() -> None:
    e = latency_evidence(
        np.zeros(300), np.full(300, 30.0), 0.5, metric="self_time", config=CFG, seed=0
    )
    assert e.rel_delta is None
    rel = next(g for g in e.gates if g.name == "relative_increase")
    assert rel.passed
    assert rel.observed is None
    assert rel.note is not None
    assert "unbounded" in rel.note
    assert e.regressed
    assert e.label == "self_time_p50"


def test_latency_evidence_is_seeded_and_deterministic() -> None:
    a = latency_evidence(BASE, POST + 30, 0.95, metric="latency", config=CFG, seed=3)
    b = latency_evidence(BASE, POST + 30, 0.95, metric="latency", config=CFG, seed=3)
    c = latency_evidence(BASE, POST + 30, 0.95, metric="latency", config=CFG, seed=4)
    assert a == b
    assert (a.ci_low_ms, a.ci_high_ms) != (c.ci_low_ms, c.ci_high_ms)
    assert a.bootstrap_seed == 3


def test_unknown_metric_names_rejected() -> None:
    with pytest.raises(ValueError):
        latency_evidence(BASE, POST, 0.5, metric="nope", config=CFG, seed=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        error_rate_evidence(1, 10, 2, 10, metric="nope", config=CFG)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- error gates


def test_error_gate_regresses_on_clear_increase() -> None:
    e = error_rate_evidence(10, 2000, 80, 2000, metric="error_rate", config=CFG)
    assert e.regressed
    assert e.abs_delta_pp == pytest.approx(3.5)
    assert e.rel_delta == pytest.approx(7.0)
    assert e.p_value < 1e-10
    assert e.z > 0


def test_zero_baseline_error_rate_relative_gate_follows_absolute_gate() -> None:
    # 0% → 2%: relative increase unbounded, decided by the absolute gate (passes)
    e = error_rate_evidence(0, 1000, 20, 1000, metric="error_rate", config=CFG)
    rel = next(g for g in e.gates if g.name == "relative_increase")
    assert e.rel_delta is None
    assert rel.passed
    assert rel.note is not None
    assert e.regressed
    # 0% → 0.2%: absolute gate fails, so the relative gate fails with it
    small = error_rate_evidence(0, 1000, 2, 1000, metric="error_rate", config=CFG)
    assert not _gate(small, "absolute_increase_pp")
    assert not _gate(small, "relative_increase")
    assert not small.regressed


def test_error_gate_needs_significance() -> None:
    # 0/200 → 2/200 (+1 pp, unbounded relative) but z = 1.42, p ≈ 0.078 ≥ 0.01
    e = error_rate_evidence(0, 200, 2, 200, metric="error_rate", config=CFG)
    assert _gate(e, "absolute_increase_pp")
    assert _gate(e, "relative_increase")
    assert not _gate(e, "z_test_p_value")
    assert e.p_value == pytest.approx(0.078, abs=0.001)
    assert not e.regressed


def test_error_gate_relative_and_absolute_thresholds() -> None:
    # 10% → 12% on large n: significant, +2 pp, but only +20% relative
    rel_fail = error_rate_evidence(1000, 10000, 1200, 10000, metric="error_rate", config=CFG)
    assert _gate(rel_fail, "z_test_p_value")
    assert _gate(rel_fail, "absolute_increase_pp")
    assert not _gate(rel_fail, "relative_increase")
    assert not rel_fail.regressed
    # 0.01% → 0.05% on huge n: significant and +400%, but only +0.04 pp
    abs_fail = error_rate_evidence(10, 100000, 50, 100000, metric="error_rate", config=CFG)
    assert _gate(abs_fail, "z_test_p_value")
    assert _gate(abs_fail, "relative_increase")
    assert not _gate(abs_fail, "absolute_increase_pp")
    assert not abs_fail.regressed


# ----------------------------------------------------------------------------- status


def test_sample_gate_short_circuits_everything() -> None:
    ev = assess_service(
        _stats(BASE[:199], minutes=30), _stats(POST + 500, minutes=13), config=CFG, seed=0
    )
    assert ev.status is ServiceStatus.INSUFFICIENT_DATA
    assert not ev.sample_gate.passed
    assert ev.sample_gate.baseline_requests == 199
    assert ev.latency == ()
    assert ev.self_time == ()
    assert ev.error_rate is None
    assert ev.severity is None  # not assessed, rather than "HEALTHY"
    assert ev.baseline.requests == 199  # description is still reported


def test_throughput_drop_is_warning_with_zero_severity() -> None:
    # 3000 requests / 30 min = 100 rpm → 600 / 13 min ≈ 46 rpm (-54%)
    ev = assess_service(
        _stats(BASE, minutes=30), _stats(POST[:600], minutes=13), config=CFG, seed=0
    )
    assert ev.status is ServiceStatus.WARNING
    assert ev.throughput is not None
    assert ev.throughput.dropped
    assert ev.throughput.rel_change == pytest.approx(600 / 13 / 100 - 1)
    assert ev.regressed_metrics == ()
    assert ev.severity is not None
    assert ev.severity.score == 0.0
    assert ev.severity.band is SeverityBand.HEALTHY  # band and status are distinct


def test_stable_traffic_is_healthy() -> None:
    ev = assess_service(_stats(BASE, minutes=30), _stats(POST, minutes=13), config=CFG, seed=0)
    assert ev.status is ServiceStatus.HEALTHY
    assert ev.severity is not None
    assert ev.severity.score == 0.0
    assert not ev.self_time_regressed
    assert not ev.originates_errors


def test_p99_only_regression_is_regressed_with_low_band() -> None:
    post = np.sort(POST)
    post[-26:] += 500.0  # top 2% only: p99 moves, p95 does not
    ev = assess_service(_stats(BASE, minutes=30), _stats(post, minutes=13), config=CFG, seed=0)
    assert ev.status is ServiceStatus.REGRESSED
    assert ev.regressed_metrics == ("latency_p99",)
    assert ev.severity is not None
    assert ev.severity.score == 0.0  # severity v1 is defined on p95 and errors only
    assert ev.severity.band is SeverityBand.HEALTHY


def test_self_time_is_evidence_not_status() -> None:
    # Total latency up 250 ms, own self-time unchanged: the time was spent waiting on a callee.
    waiting = assess_service(
        _stats(BASE, minutes=30, self_time=BASE / 4),
        _stats(POST + 250, minutes=13, self_time=POST / 4),
        config=CFG,
        seed=0,
    )
    assert waiting.status is ServiceStatus.REGRESSED
    assert not waiting.self_time_regressed
    assert [e.metric for e in waiting.self_time] == ["self_time"] * 3

    # Self-time up, total latency (noisy, large) not past its gates: still HEALTHY.
    own = assess_service(
        _stats(BASE * 20, minutes=30, self_time=BASE),
        _stats(POST * 20, minutes=13, self_time=POST + 250),
        config=CFG,
        seed=0,
    )
    assert own.status is ServiceStatus.HEALTHY
    assert own.self_time_regressed


def test_self_time_regressed_ignores_median_only_shift() -> None:
    post_self = np.sort(POST)
    post_self[: len(post_self) // 2] += 60  # lower half shifts: p50 rises, tail does not
    ev = assess_service(
        _stats(BASE, minutes=30),
        _stats(POST, minutes=13, self_time=post_self),
        config=CFG,
        seed=0,
    )
    p50 = ev.self_time_at(0.5)
    assert p50 is not None
    assert p50.regressed
    assert not ev.self_time_regressed


def test_originates_errors_requires_own_error_regression() -> None:
    base = _stats(BASE, minutes=30, errors=6, own_errors=6)  # 0.2% own background errors
    propagated = _stats(POST, minutes=13, errors=60, own_errors=3)  # callee failures
    ev = assess_service(base, propagated, config=CFG, seed=0)
    assert ev.error_rate is not None
    assert ev.error_rate.regressed
    assert ev.own_error_rate is not None
    assert not ev.own_error_rate.regressed
    assert not ev.originates_errors

    own = _stats(POST, minutes=13, errors=60, own_errors=60)
    assert assess_service(base, own, config=CFG, seed=0).originates_errors


def test_service_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="different services"):
        assess_service(_stats(BASE, minutes=30), _stats(POST, minutes=13, service="x"), config=CFG)


def test_evidence_json_roundtrip() -> None:
    ev = assess_service(
        _stats(BASE, minutes=30, errors=2), _stats(POST + 250, minutes=13, errors=40), config=CFG
    )
    text = ev.model_dump_json()
    assert "NaN" not in text
    assert "Infinity" not in text
    json.loads(text)
    assert ServiceEvidence.model_validate_json(text) == ev


# ----------------------------------------------------------------------------- severity v1


def test_severity_formula() -> None:
    lat = latency_evidence(
        np.full(300, 100.0), np.full(300, 200.0), 0.95, metric="latency", config=CFG, seed=0
    )
    err = error_rate_evidence(10, 1000, 40, 1000, metric="error_rate", config=CFG)
    sev = severity_v1(lat, err)
    assert sev.latency_component == pytest.approx(20.0)  # 20·log2(200/100)
    assert sev.error_component == pytest.approx(24.0)  # 8·(4 - 1) pp
    assert sev.score == pytest.approx(44.0)
    assert sev.band is SeverityBand.HIGH
    assert sev.formula == "severity-v1"


def test_severity_components_are_clamped_and_gated() -> None:
    lat = latency_evidence(
        np.full(300, 100.0), np.full(300, 1600.0), 0.95, metric="latency", config=CFG, seed=0
    )
    err = error_rate_evidence(0, 1000, 200, 1000, metric="error_rate", config=CFG)
    sev = severity_v1(lat, err)
    assert sev.latency_component == 50.0  # 20·log2(16) = 80 → 50
    assert sev.error_component == 50.0  # 8·20 = 160 → 50
    assert sev.score == 100.0
    assert sev.band is SeverityBand.CRITICAL
    # a failed gate contributes nothing, however large the raw change
    not_significant = error_rate_evidence(0, 50, 3, 50, metric="error_rate", config=CFG)
    assert not not_significant.regressed
    assert severity_v1(None, not_significant).score == 0.0
    # zero baseline p95 with a passed gate scores the cap
    zero = latency_evidence(
        np.zeros(300), np.full(300, 50.0), 0.95, metric="latency", config=CFG, seed=0
    )
    assert severity_v1(zero, None).latency_component == 50.0


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0.0, SeverityBand.HEALTHY),
        (14.999, SeverityBand.HEALTHY),
        (15.0, SeverityBand.WARNING),
        (39.999, SeverityBand.WARNING),
        (40.0, SeverityBand.HIGH),
        (69.999, SeverityBand.HIGH),
        (70.0, SeverityBand.CRITICAL),
        (100.0, SeverityBand.CRITICAL),
    ],
)
def test_band_thresholds(score: float, band: SeverityBand) -> None:
    assert band_for(score) is band
