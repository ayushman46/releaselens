from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from rl_core.regression.evidence import OnsetEvidence
from rl_core.regression.metrics import ServiceSamples, window_stats
from rl_core.regression.onset import bootstrap_quantile_interval, estimate_onset
from rl_core.regression.windows import WindowConfig
from rl_core.telemetry.model import datetime_to_ns

T = datetime(2026, 10, 13, 14, 2, tzinfo=UTC)
T_NS = datetime_to_ns(T)
MIN = 60 * 1_000_000_000
CFG = WindowConfig()


def _timeline(per_minute: int, slow_from_minute: int | None) -> ServiceSamples:
    """Requests from T-30 m to T+15 m, evenly spaced, +200 ms from ``slow_from_minute`` on.

    Latencies repeat the pattern 100..109 ms, so every full minute has exactly the baseline's
    distribution: no sampling noise. (With random latencies a no-change minute's p95 clears the
    baseline-p95 CI upper bound in roughly a fifth to a quarter of minutes — measured, see the
    M8 report — which would make an "exact onset minute" assertion flaky.)
    """
    starts = []
    latency = []
    for minute in range(-30, 15):
        for i in range(per_minute):
            starts.append(T_NS + minute * MIN + i * (MIN // per_minute))
            value = 100.0 + (i % 10)
            if slow_from_minute is not None and minute >= slow_from_minute:
                value += 200.0
            latency.append(value)
    lat = np.asarray(latency)
    n = lat.size
    return ServiceSamples(
        service="svc",
        start_ns=np.asarray(starts, dtype=np.int64),
        latency_ms=lat,
        self_time_ms=lat,
        is_error=np.zeros(n, dtype=np.bool_),
        is_own_error=np.zeros(n, dtype=np.bool_),
    )


def _onset(
    samples: ServiceSamples, as_of: datetime | None = None, config: WindowConfig = CFG
) -> OnsetEvidence:
    w = config.windows(T, as_of=as_of)
    base = window_stats(samples, w.baseline_start_ns, w.baseline_end_ns, end_inclusive=False)
    return estimate_onset(samples, base, w, config=config, seed=0)


def test_onset_is_first_minute_above_baseline_ci() -> None:
    ev = _onset(_timeline(60, slow_from_minute=5))
    assert ev.onset_at == T + timedelta(minutes=5)
    assert ev.minutes_after_deploy == 5
    assert len(ev.buckets) == 15  # [T, T+15m], warm-up included
    assert ev.buckets[0].start == T
    assert not any(b.exceeds for b in ev.buckets[:5])
    assert all(b.exceeds for b in ev.buckets[5:])
    assert ev.baseline_p95_ci_high_ms >= ev.baseline_p95_ms
    assert ev.bootstrap_resamples == 1000


def test_onset_includes_warmup_minutes() -> None:
    assert _onset(_timeline(60, slow_from_minute=0)).minutes_after_deploy == 0


def test_no_onset_without_change() -> None:
    ev = _onset(_timeline(60, slow_from_minute=None))
    assert ev.onset_at is None
    assert ev.minutes_after_deploy is None
    assert not any(b.exceeds for b in ev.buckets)


def test_buckets_with_too_few_samples_are_skipped() -> None:
    ev = _onset(_timeline(10, slow_from_minute=0))  # 10 < 20 samples per minute
    assert ev.onset_at is None
    assert all(b.p95_ms is None and not b.exceeds for b in ev.buckets)
    lenient = _onset(_timeline(10, slow_from_minute=0), config=WindowConfig(onset_min_samples=5))
    assert lenient.minutes_after_deploy == 0


def test_partial_window_and_analysis_before_deploy() -> None:
    ev = _onset(_timeline(60, slow_from_minute=5), as_of=T + timedelta(minutes=5, seconds=30))
    assert len(ev.buckets) == 6
    assert ev.buckets[-1].samples == 31  # [T+5m, T+5m30s] inclusive end
    at_deploy = _onset(_timeline(60, slow_from_minute=0), as_of=T)
    assert len(at_deploy.buckets) == 1  # the single instant T
    assert at_deploy.buckets[0].samples == 1


def test_bootstrap_quantile_interval() -> None:
    data = np.random.default_rng(3).lognormal(4, 0.5, 2000)
    lo, hi = bootstrap_quantile_interval(data, 0.95, seed=1)
    assert lo < float(np.quantile(data, 0.95)) < hi
    assert bootstrap_quantile_interval(data, 0.95, seed=1) == (lo, hi)
    assert bootstrap_quantile_interval([5.0] * 10, 0.5) == (5.0, 5.0)
    with pytest.raises(ValueError):
        bootstrap_quantile_interval([], 0.5)
    with pytest.raises(ValueError):
        bootstrap_quantile_interval([1.0], 1.0)
    with pytest.raises(ValueError):
        bootstrap_quantile_interval([1.0], 0.5, n_resamples=0)
