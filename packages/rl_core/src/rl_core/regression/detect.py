"""Per-service regression gates, service status and severity v1 (spec M8).

A metric is called **regressed only when every one of its gates passes**:

* **Sample gate** (service level, first): at least ``min_requests`` requests in *each* window,
  otherwise the service is INSUFFICIENT_DATA and no other gate is evaluated.
* **Latency**, per configured percentile ``q`` (on total latency, and separately on service
  self-time):
  ``relative_increase`` ``(post_q - base_q)/base_q ≥ latency_rel_threshold`` **and**
  ``absolute_increase_ms`` ``post_q - base_q ≥ latency_abs_threshold_ms`` **and**
  ``bootstrap_ci_low_above_zero``: the lower bound of the seeded percentile-bootstrap CI of
  ``post_q - base_q`` (:func:`rl_core.stats.bootstrap_quantile_diff`) is ``> 0``.
  With ``base_q == 0`` the relative change is unbounded: the relative gate passes iff
  ``post_q > base_q`` and ``rel_delta`` is reported as ``None``.
* **Error rate**: one-sided pooled two-proportion z-test (H1: post > baseline,
  :func:`rl_core.stats.two_proportion_ztest`) ``p < error_p_threshold`` **and** one-sided Fisher
  exact test ``p < error_p_threshold`` **and** absolute increase ``≥ error_abs_threshold_pp``
  percentage points **and** relative increase ``≥ error_rel_threshold``. With a zero baseline error
  rate the relative increase is unbounded: the relative gate passes iff the absolute gate passes,
  and ``rel_delta`` is ``None``.

  Why the exact test as well: services run at error rates of a fraction of a percent, so a window
  often holds only a handful of errors. There the z-test's normal approximation is
  anti-conservative (e.g. 2/1837 → 5/751 gives z-test p = 0.0066 but exact p = 0.025), which made
  the no-change control scenario raise error-rate false alarms. With large counts both tests agree
  and the exact gate changes nothing.

  The absolute and relative gates are decided in exact rational arithmetic on the counts, so a
  change exactly at a threshold passes (in floating point 2/200 → 3/200 is 0.49999999999999994 pp
  and failed the ``≥ 0.5 pp`` gate). The reported ``observed`` values are the nearest floats.
* **Throughput**: a drop ``(base_rpm - post_rpm)/base_rpm ≥ throughput_drop_threshold`` makes the
  service WARNING, never REGRESSED on its own (traffic can drop for reasons unrelated to the
  release, e.g. a client-side change or time of day).

**Service status**: REGRESSED if total latency at any configured percentile or the error rate is
regressed; else WARNING if throughput dropped; else HEALTHY. Self-time and own-error gates do not
change the status — they are evidence for probable-origin scoring (:mod:`rl_core.triage`):

* ``self_time_regressed``: the self-time gates passed at a tail percentile (``q ≥ 0.95``, i.e. p95
  or p99 with the default config). A median-only self-time shift is reported but not used.
* ``originates_errors``: the error-rate gate passed **and** the same gates pass on the service's
  *own* errors (failed requests whose callees did not fail). Requiring the own-error rate to
  regress, rather than "at least one own error exists", stops a caller whose ordinary background
  errors continue while it propagates a callee's failures from being credited as an error origin.

One seed is used for every bootstrap of an analysis (recorded in the evidence); identical inputs
give identical intervals.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Literal

import numpy as np
import numpy.typing as npt
from scipy import stats as _st

from rl_core.regression.evidence import (
    ErrorRateEvidence,
    Gate,
    LatencyEvidence,
    SampleGate,
    ServiceEvidence,
    ServiceStatus,
    Severity,
    SeverityBand,
    ThroughputEvidence,
    WindowSummary,
)
from rl_core.regression.metrics import WindowStats
from rl_core.regression.windows import SEVERITY_PERCENTILE, WindowConfig
from rl_core.stats import bootstrap_quantile_diff, two_proportion_ztest

#: Self-time percentiles at or above this count for ``self_time_regressed`` (p95, p99).
TAIL_PERCENTILE = 0.95

SEVERITY_LATENCY_WEIGHT = 20.0
SEVERITY_ERROR_WEIGHT = 8.0
SEVERITY_COMPONENT_CAP = 50.0
SEVERITY_MAX = 100.0
BAND_WARNING = 15.0
BAND_HIGH = 40.0
BAND_CRITICAL = 70.0

#: Upper bound on resampled values materialised at once per sample (``chunk * n``): ~16 MB of
#: int64 indices plus the same again of gathered floats. The bootstrap's default chunk of 200
#: resamples is kept up to 10 000 requests per window (so results there are unchanged); beyond
#: that the chunk shrinks. With a fixed chunk, 100 000 baseline + 43 000 post requests peaked at
#: ~0.8 GB RSS per bootstrap, which would not fit a worker on the 4 GiB development VM at high
#: traffic.
BOOTSTRAP_MAX_ELEMENTS = 2_000_000
BOOTSTRAP_MAX_CHUNK = 200


def bootstrap_chunk(*sample_sizes: int) -> int:
    """Resamples per bootstrap chunk that keep ``chunk * max(sample_sizes)`` within budget.

    Deterministic in the sample sizes, so a given input always uses the same chunking (the
    random stream, and therefore the interval, depends on it).
    """
    largest = max(1, *sample_sizes)
    return max(1, min(BOOTSTRAP_MAX_CHUNK, BOOTSTRAP_MAX_ELEMENTS // largest))


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _exact(threshold: float) -> Fraction:
    """A configured threshold as the decimal it was written as (``0.3`` → 3/10, not 0.2999…)."""
    return Fraction(repr(threshold))


def _finite_sample(values: npt.ArrayLike, name: str) -> npt.NDArray[np.float64]:
    sample = np.asarray(values, dtype=np.float64)
    if sample.ndim != 1:
        raise ValueError(f"{name} must be a 1-D sample")
    if not np.all(np.isfinite(sample)):
        # NaN/inf would propagate into every percentile and CI, and the evidence could not be
        # stored: JSON has no NaN, so the fields would serialise as null and fail validation.
        raise ValueError(f"{name} contains NaN or infinite values")
    return sample


# ----------------------------------------------------------------------------- latency
def latency_evidence(
    baseline_ms: npt.ArrayLike,
    post_ms: npt.ArrayLike,
    percentile: float,
    *,
    metric: Literal["latency", "self_time"],
    config: WindowConfig,
    seed: int,
) -> LatencyEvidence:
    """Evaluate the three latency gates for one percentile of one distribution."""
    if metric not in ("latency", "self_time"):
        raise ValueError(f"unknown latency metric {metric!r}")
    base_sample = _finite_sample(baseline_ms, "baseline sample")
    post_sample = _finite_sample(post_ms, "post sample")
    boot = bootstrap_quantile_diff(
        base_sample,
        post_sample,
        percentile,
        n_resamples=config.bootstrap_resamples,
        confidence=config.bootstrap_confidence,
        seed=seed,
        chunk=bootstrap_chunk(base_sample.size, post_sample.size),
    )
    base, post, delta = boot.baseline, boot.post, boot.diff

    if base > 0:
        rel: float | None = delta / base
        rel_gate = Gate(
            name="relative_increase",
            passed=delta / base >= config.latency_rel_threshold,
            observed=rel,
            threshold=config.latency_rel_threshold,
            op=">=",
        )
    else:
        rel = None
        rel_gate = Gate(
            name="relative_increase",
            passed=delta > 0,
            observed=None,
            threshold=config.latency_rel_threshold,
            op=">=",
            note="baseline percentile is 0 ms: relative increase is unbounded; passes iff the "
            "percentile increased",
        )
    gates = (
        rel_gate,
        Gate(
            name="absolute_increase_ms",
            passed=delta >= config.latency_abs_threshold_ms,
            observed=delta,
            threshold=config.latency_abs_threshold_ms,
            op=">=",
        ),
        Gate(
            name="bootstrap_ci_low_above_zero",
            passed=boot.low > 0.0,
            observed=boot.low,
            threshold=0.0,
            op=">",
        ),
    )
    return LatencyEvidence(
        metric=metric,
        percentile=percentile,
        baseline_ms=base,
        post_ms=post,
        abs_delta_ms=delta,
        rel_delta=rel,
        ci_low_ms=boot.low,
        ci_high_ms=boot.high,
        ci_confidence=boot.confidence,
        bootstrap_resamples=boot.n_resamples,
        bootstrap_seed=boot.seed,
        n_baseline=int(base_sample.size),
        n_post=int(post_sample.size),
        gates=gates,
        regressed=all(g.passed for g in gates),
    )


# ----------------------------------------------------------------------------- errors
def error_rate_evidence(
    baseline_errors: int,
    baseline_requests: int,
    post_errors: int,
    post_requests: int,
    *,
    metric: Literal["error_rate", "own_error_rate"],
    config: WindowConfig,
) -> ErrorRateEvidence:
    """Evaluate the four error-rate gates (z-test, exact test, absolute pp, relative)."""
    if metric not in ("error_rate", "own_error_rate"):
        raise ValueError(f"unknown error metric {metric!r}")
    test = two_proportion_ztest(
        baseline_errors, baseline_requests, post_errors, post_requests, alternative="larger"
    )
    exact_p = fisher_exact_greater(baseline_errors, baseline_requests, post_errors, post_requests)
    # Exact rationals for the threshold decisions (see module docstring).
    base_rate = Fraction(baseline_errors, baseline_requests)
    diff = Fraction(post_errors, post_requests) - base_rate
    abs_pp = float(diff * 100)
    abs_gate = Gate(
        name="absolute_increase_pp",
        passed=diff * 100 >= _exact(config.error_abs_threshold_pp),
        observed=abs_pp,
        threshold=config.error_abs_threshold_pp,
        op=">=",
    )
    if base_rate > 0:
        rel: float | None = float(diff / base_rate)
        rel_gate = Gate(
            name="relative_increase",
            passed=diff / base_rate >= _exact(config.error_rel_threshold),
            observed=rel,
            threshold=config.error_rel_threshold,
            op=">=",
        )
    else:
        rel = None
        rel_gate = Gate(
            name="relative_increase",
            passed=abs_gate.passed,
            observed=None,
            threshold=config.error_rel_threshold,
            op=">=",
            note="baseline error rate is 0: relative increase is unbounded; passes iff the "
            "absolute gate passes",
        )
    gates = (
        Gate(
            name="z_test_p_value",
            passed=test.p_value < config.error_p_threshold,
            observed=test.p_value,
            threshold=config.error_p_threshold,
            op="<",
        ),
        Gate(
            name="fisher_exact_p_value",
            passed=exact_p < config.error_p_threshold,
            observed=exact_p,
            threshold=config.error_p_threshold,
            op="<",
        ),
        abs_gate,
        rel_gate,
    )
    return ErrorRateEvidence(
        metric=metric,
        baseline_errors=baseline_errors,
        baseline_requests=baseline_requests,
        post_errors=post_errors,
        post_requests=post_requests,
        baseline_rate=test.p1,
        post_rate=test.p2,
        abs_delta_pp=abs_pp,
        rel_delta=rel,
        z=test.z,
        p_value=test.p_value,
        exact_p_value=exact_p,
        gates=gates,
        regressed=all(g.passed for g in gates),
    )


def fisher_exact_greater(
    baseline_errors: int, baseline_requests: int, post_errors: int, post_requests: int
) -> float:
    """One-sided Fisher exact test p-value of H1: post error rate > baseline error rate.

    Conditions on the total number of errors (hypergeometric), so it stays valid with the handful
    of errors a healthy service produces in a window, where the z-test's normal approximation is
    not.
    """
    for errors, requests in ((baseline_errors, baseline_requests), (post_errors, post_requests)):
        if requests <= 0:
            raise ValueError("sample sizes must be positive")
        if not 0 <= errors <= requests:
            raise ValueError("error counts must be within [0, requests]")
    table = [
        [post_errors, post_requests - post_errors],
        [baseline_errors, baseline_requests - baseline_errors],
    ]
    return float(_st.fisher_exact(table, alternative="greater").pvalue)


# ----------------------------------------------------------------------------- throughput
def throughput_evidence(
    baseline: WindowStats, post: WindowStats, *, config: WindowConfig
) -> ThroughputEvidence:
    base, cur = baseline.throughput_rpm, post.throughput_rpm
    rel: float | None = None
    drop: float | None = None
    if base is not None and base > 0 and cur is not None:
        rel = (cur - base) / base
        drop = -rel
    dropped = drop is not None and drop >= config.throughput_drop_threshold
    return ThroughputEvidence(
        baseline_rpm=base,
        post_rpm=cur,
        rel_change=rel,
        gate=Gate(
            name="throughput_drop",
            passed=dropped,
            observed=drop,
            threshold=config.throughput_drop_threshold,
            op=">=",
        ),
        dropped=dropped,
    )


# ----------------------------------------------------------------------------- severity
def band_for(score: float) -> SeverityBand:
    if score >= BAND_CRITICAL:
        return SeverityBand.CRITICAL
    if score >= BAND_HIGH:
        return SeverityBand.HIGH
    if score >= BAND_WARNING:
        return SeverityBand.WARNING
    return SeverityBand.HEALTHY


def severity_v1(latency_p95: LatencyEvidence | None, errors: ErrorRateEvidence | None) -> Severity:
    """Severity v1 (see :class:`rl_core.regression.evidence.Severity`). Not a probability.

    A zero baseline p95 with a passed gate makes the ratio unbounded; it scores the latency cap.
    """
    s_lat = 0.0
    if latency_p95 is not None and latency_p95.regressed:
        if latency_p95.baseline_ms > 0:
            ratio = latency_p95.post_ms / latency_p95.baseline_ms
            s_lat = _clamp(SEVERITY_LATENCY_WEIGHT * math.log2(ratio), 0.0, SEVERITY_COMPONENT_CAP)
        else:
            s_lat = SEVERITY_COMPONENT_CAP
    s_err = 0.0
    if errors is not None and errors.regressed:
        s_err = _clamp(SEVERITY_ERROR_WEIGHT * errors.abs_delta_pp, 0.0, SEVERITY_COMPONENT_CAP)
    score = min(SEVERITY_MAX, s_lat + s_err)
    return Severity(
        score=score, band=band_for(score), latency_component=s_lat, error_component=s_err
    )


# ----------------------------------------------------------------------------- service
def summarise(stats: WindowStats) -> WindowSummary:
    return WindowSummary(
        requests=stats.request_count,
        errors=stats.error_count,
        error_rate=stats.error_rate,
        throughput_rpm=stats.throughput_rpm,
        latency_p50_ms=stats.latency_quantile(0.5),
        latency_p95_ms=stats.latency_quantile(0.95),
        latency_p99_ms=stats.latency_quantile(0.99),
        self_time_p95_ms=stats.self_time_quantile(0.95),
    )


def assess_service(
    baseline: WindowStats,
    post: WindowStats,
    *,
    config: WindowConfig,
    seed: int | None = None,
) -> ServiceEvidence:
    """Run every gate for one service and derive its status and severity."""
    if baseline.service != post.service:
        raise ValueError("baseline and post windows belong to different services")
    seed = config.bootstrap_seed if seed is None else seed
    sample_gate = SampleGate(
        baseline_requests=baseline.request_count,
        post_requests=post.request_count,
        min_requests=config.min_requests,
        passed=baseline.request_count >= config.min_requests
        and post.request_count >= config.min_requests,
    )
    if not sample_gate.passed:
        return ServiceEvidence(
            service=baseline.service,
            variant=config.name,
            status=ServiceStatus.INSUFFICIENT_DATA,
            sample_gate=sample_gate,
            baseline=summarise(baseline),
            post=summarise(post),
        )

    latency = tuple(
        latency_evidence(
            baseline.latency_ms, post.latency_ms, q, metric="latency", config=config, seed=seed
        )
        for q in config.percentiles
    )
    self_time = tuple(
        latency_evidence(
            baseline.self_time_ms,
            post.self_time_ms,
            q,
            metric="self_time",
            config=config,
            seed=seed,
        )
        for q in config.percentiles
    )
    errors = error_rate_evidence(
        baseline.error_count,
        baseline.request_count,
        post.error_count,
        post.request_count,
        metric="error_rate",
        config=config,
    )
    own_errors = error_rate_evidence(
        baseline.own_error_count,
        baseline.request_count,
        post.own_error_count,
        post.request_count,
        metric="own_error_rate",
        config=config,
    )
    throughput = throughput_evidence(baseline, post, config=config)

    regressed_metrics = tuple(e.label for e in latency if e.regressed) + (
        ("error_rate",) if errors.regressed else ()
    )
    if regressed_metrics:
        status = ServiceStatus.REGRESSED
    elif throughput.dropped:
        status = ServiceStatus.WARNING
    else:
        status = ServiceStatus.HEALTHY

    p95 = next(e for e in latency if e.percentile == SEVERITY_PERCENTILE)
    return ServiceEvidence(
        service=baseline.service,
        variant=config.name,
        status=status,
        sample_gate=sample_gate,
        baseline=summarise(baseline),
        post=summarise(post),
        latency=latency,
        self_time=self_time,
        error_rate=errors,
        own_error_rate=own_errors,
        throughput=throughput,
        severity=severity_v1(p95, errors),
        regressed_metrics=regressed_metrics,
        self_time_regressed=any(e.regressed for e in self_time if e.percentile >= TAIL_PERCENTILE),
        originates_errors=errors.regressed and own_errors.regressed,
    )
