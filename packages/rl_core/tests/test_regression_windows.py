from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from rl_core.regression.windows import WindowConfig
from rl_core.telemetry.model import datetime_to_ns

T = datetime(2026, 10, 13, 14, 2, tzinfo=UTC)
MIN = 60 * 1_000_000_000


def test_spec_defaults() -> None:
    c = WindowConfig()
    assert (c.baseline_minutes, c.warmup_minutes, c.post_end_minutes) == (30, 2, 15)
    assert c.min_requests == 200
    assert (c.latency_rel_threshold, c.latency_abs_threshold_ms) == (0.25, 20.0)
    assert (c.bootstrap_resamples, c.bootstrap_seed, c.bootstrap_confidence) == (1000, 0, 0.95)
    assert (c.error_p_threshold, c.error_abs_threshold_pp, c.error_rel_threshold) == (
        0.01,
        0.5,
        0.5,
    )
    assert c.throughput_drop_threshold == 0.30
    assert c.percentiles == (0.5, 0.95, 0.99)


def test_window_bounds_and_membership() -> None:
    w = WindowConfig().windows(T)
    t = datetime_to_ns(T)
    assert w.baseline_start == T - timedelta(minutes=30)
    assert w.baseline_end == T
    assert w.post_start == T + timedelta(minutes=2)
    assert w.post_end == w.effective_post_end == T + timedelta(minutes=15)
    assert w.baseline_minutes == 30
    assert w.post_minutes == 13

    assert w.in_baseline(t - 30 * MIN)  # start inclusive
    assert not w.in_baseline(t)  # end exclusive
    assert not w.in_post(t + 2 * MIN - 1)  # warm-up excluded
    assert w.in_post(t + 2 * MIN)
    assert w.in_post(t + 15 * MIN)  # end inclusive (spec)
    assert not w.in_post(t + 15 * MIN + 1)


def test_as_of_truncates_post_window() -> None:
    w = WindowConfig().windows(T, as_of=T + timedelta(minutes=5))
    assert w.effective_post_end == T + timedelta(minutes=5)
    assert w.post_minutes == 3
    early = WindowConfig().windows(T, as_of=T + timedelta(minutes=1))
    assert early.post_minutes == 0
    late = WindowConfig().windows(T, as_of=T + timedelta(hours=1))
    assert late.effective_post_end == late.post_end
    with pytest.raises(ValueError, match="precede"):
        WindowConfig().windows(T, as_of=T - timedelta(seconds=1))


def test_windows_require_aware_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        WindowConfig().windows(datetime(2026, 10, 13, 14, 2))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"percentiles": (0.5, 0.99)},  # severity/onset need p95
        {"percentiles": (0.95, 1.0)},
        {"percentiles": (0.95, 0.95)},
        {"bootstrap_resamples": 999},  # spec: at least 1000
        {"min_requests": 0},
        {"warmup_minutes": 15, "post_end_minutes": 15},
        {"unknown_threshold": 1},
    ],
)
def test_invalid_configs_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WindowConfig(**kwargs)


def test_percentiles_sorted() -> None:
    assert WindowConfig(percentiles=(0.99, 0.95, 0.5)).percentiles == (0.5, 0.95, 0.99)


def test_relaxed_halves_effect_sizes_only() -> None:
    strict = WindowConfig()
    relaxed = strict.relaxed()
    assert relaxed.name == "relaxed"
    assert relaxed.latency_rel_threshold == pytest.approx(0.125)
    assert relaxed.latency_abs_threshold_ms == pytest.approx(10.0)
    assert relaxed.error_abs_threshold_pp == pytest.approx(0.25)
    assert relaxed.error_rel_threshold == pytest.approx(0.25)
    # evidence requirements unchanged
    assert relaxed.min_requests == strict.min_requests
    assert relaxed.error_p_threshold == strict.error_p_threshold
    assert relaxed.bootstrap_confidence == strict.bootstrap_confidence
    assert relaxed.bootstrap_resamples == strict.bootstrap_resamples
    with pytest.raises(ValueError, match="factor"):
        strict.relaxed(0.0)


def test_config_and_windows_json_roundtrip() -> None:
    c = WindowConfig()
    assert WindowConfig.model_validate_json(c.model_dump_json()) == c
    w = c.windows(T, as_of=T + timedelta(minutes=10))
    again = type(w).model_validate_json(w.model_dump_json())
    assert again == w
    assert again.post_start_ns == w.post_start_ns
