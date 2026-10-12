"""Estimated onset time of a latency regression.

Definition (spec M8): split ``[T, effective post end]`` — the warm-up *included*, because a
regression usually starts at the deploy instant, not after the warm-up — into consecutive
``onset_bucket_seconds`` buckets ``[T + k·b, T + (k+1)·b)``; the last bucket also contains the
inclusive post-window end. The onset is the start of the **first bucket with at least
``onset_min_samples`` requests whose p95 exceeds the upper bound of a seeded bootstrap CI of the
baseline p95** (``bootstrap_confidence``, default 95%). ``None`` when no bucket qualifies.

The comparison is against the CI's *upper bound* rather than the baseline point estimate, so a
bucket must be above what the baseline itself could plausibly have produced. A single noisy
bucket can still exceed it; that is why onset is only computed for services whose **p95 latency
regression** was already established by the gates (:func:`has_latency_onset`), and it is only
ever an *estimate* of when the change started.

Why p95 specifically, and not "any REGRESSED service": the buckets are compared on p95. For a
service that regressed on errors only (or only at p99), its p95 did not move, so any bucket that
exceeds the CI does so by chance — and a one-minute bucket's p95 is far noisier than the 30-minute
baseline's, so many do. Such a service would otherwise receive a spurious "latency onset" (and the
triage point that goes with it).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from rl_core.regression.detect import bootstrap_chunk
from rl_core.regression.evidence import OnsetBucket, OnsetEvidence, ServiceEvidence
from rl_core.regression.metrics import ServiceSamples, WindowStats, quantile
from rl_core.regression.windows import SEVERITY_PERCENTILE, AnalysisWindows, WindowConfig
from rl_core.telemetry.model import NS_PER_S, ns_to_datetime


def bootstrap_quantile_interval(
    sample: npt.ArrayLike,
    q: float,
    *,
    n_resamples: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
    chunk: int = 200,
) -> tuple[float, float]:
    """Percentile-bootstrap CI of ``Q_q(sample)`` (one sample; seeded PCG64, chunked).

    The single-sample counterpart of :func:`rl_core.stats.bootstrap_quantile_diff`, with the same
    estimator (``numpy.quantile``, linear) and the same resampling scheme.
    """
    data = np.asarray(sample, dtype=np.float64)
    if data.size == 0:
        raise ValueError("sample must be non-empty")
    if not np.all(np.isfinite(data)):
        raise ValueError("sample contains NaN or infinite values")
    if not 0 < q < 1:
        raise ValueError("q must be in (0, 1)")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")
    rng = np.random.default_rng(seed)
    estimates = np.empty(n_resamples, dtype=np.float64)
    done = 0
    while done < n_resamples:
        k = min(chunk, n_resamples - done)
        resampled = data[rng.integers(0, data.size, size=(k, data.size))]
        estimates[done : done + k] = np.quantile(resampled, q, axis=1)
        done += k
    alpha = 1 - confidence
    low, high = np.quantile(estimates, [alpha / 2, 1 - alpha / 2])
    return float(low), float(high)


def has_latency_onset(evidence: ServiceEvidence) -> bool:
    """Whether an onset estimate is meaningful: the p95 *total-latency* gates all passed."""
    p95 = evidence.latency_at(SEVERITY_PERCENTILE)
    return p95 is not None and p95.regressed


def estimate_onset(
    samples: ServiceSamples,
    baseline: WindowStats,
    windows: AnalysisWindows,
    *,
    config: WindowConfig,
    seed: int,
) -> OnsetEvidence:
    """Onset estimate for one service (see module docstring)."""
    base_p95 = quantile(baseline.latency_ms, SEVERITY_PERCENTILE)
    if base_p95 is None:
        raise ValueError("onset needs a non-empty baseline")
    _, ci_high = bootstrap_quantile_interval(
        baseline.latency_ms,
        SEVERITY_PERCENTILE,
        n_resamples=config.bootstrap_resamples,
        confidence=config.bootstrap_confidence,
        seed=seed,
        chunk=bootstrap_chunk(baseline.request_count),
    )

    t0 = windows.deployed_at_ns
    end = windows.effective_post_end_ns
    width = config.onset_bucket_seconds * NS_PER_S
    # ceil((end - t0) / width) buckets, at least one (end == t0 is a single-instant bucket);
    # none when the analysis runs before the deploy instant.
    n_buckets = 0 if end < t0 else max(1, (end - t0 + width - 1) // width)

    buckets: list[OnsetBucket] = []
    onset_index: int | None = None
    for k in range(n_buckets):
        lo = t0 + k * width
        last = k == n_buckets - 1
        hi = end if last else lo + width
        chunk = samples.select(lo, hi, hi_inclusive=last)
        n = len(chunk)
        p95 = (
            quantile(chunk.latency_ms, SEVERITY_PERCENTILE)
            if n >= config.onset_min_samples
            else None
        )
        exceeds = p95 is not None and p95 > ci_high
        if exceeds and onset_index is None:
            onset_index = k
        buckets.append(
            OnsetBucket(start=ns_to_datetime(lo), samples=n, p95_ms=p95, exceeds=exceeds)
        )

    onset_at = None if onset_index is None else ns_to_datetime(t0 + onset_index * width)
    minutes = None
    if onset_index is not None:
        minutes = (onset_index * config.onset_bucket_seconds) // 60
    return OnsetEvidence(
        onset_at=onset_at,
        minutes_after_deploy=minutes,
        baseline_p95_ms=base_p95,
        baseline_p95_ci_high_ms=ci_high,
        ci_confidence=config.bootstrap_confidence,
        bootstrap_resamples=config.bootstrap_resamples,
        bootstrap_seed=seed,
        bucket_seconds=config.onset_bucket_seconds,
        min_samples_per_bucket=config.onset_min_samples,
        buckets=tuple(buckets),
    )
