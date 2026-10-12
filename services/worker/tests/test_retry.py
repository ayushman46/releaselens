"""Backoff math: capped exponential ceilings, full jitter bounds, pause floor."""

from __future__ import annotations

import random
import statistics

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rl_worker.retry import Backoff


def test_ceiling_doubles_then_caps() -> None:
    b = Backoff(base_s=0.5, cap_s=30.0, max_attempts=5)
    assert [b.ceiling(n) for n in range(1, 9)] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_huge_attempt_numbers_do_not_overflow() -> None:
    assert Backoff(base_s=0.5, cap_s=30.0).ceiling(10_000) == 30.0


@given(
    base=st.floats(min_value=0.001, max_value=10),
    factor=st.floats(min_value=1, max_value=1000),
    attempt=st.integers(min_value=1, max_value=200),
    seed=st.integers(min_value=0, max_value=2**32),
)
def test_full_jitter_stays_within_zero_and_ceiling(
    base: float, factor: float, attempt: int, seed: int
) -> None:
    b = Backoff(base_s=base, cap_s=base * factor, rng=random.Random(seed))
    delay = b.delay(attempt)
    assert 0.0 <= delay <= b.ceiling(attempt) <= b.cap_s


def test_jitter_is_spread_over_the_whole_interval() -> None:
    # Full jitter is uniform on [0, ceiling]: mean ~ ceiling/2, and both halves are used.
    b = Backoff(base_s=1.0, cap_s=8.0, rng=random.Random(7))
    samples = [b.delay(4) for _ in range(20_000)]  # ceiling(4) = 8
    assert statistics.fmean(samples) == pytest.approx(4.0, rel=0.03)
    assert min(samples) < 0.1
    assert max(samples) > 7.9


def test_seeded_rng_is_deterministic() -> None:
    a = Backoff(rng=random.Random(42))
    b = Backoff(rng=random.Random(42))
    assert [a.delay(n) for n in range(1, 6)] == [b.delay(n) for n in range(1, 6)]


def test_pause_delay_never_collapses_into_a_tight_loop() -> None:
    b = Backoff(base_s=0.5, cap_s=30.0, rng=random.Random(3))
    pauses = [b.pause_delay() for _ in range(5_000)]
    assert min(pauses) >= 15.0
    assert max(pauses) <= 30.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_s": 0}, "base_s"),
        ({"base_s": 2, "cap_s": 1}, "cap_s"),
        ({"max_attempts": 0}, "max_attempts"),
    ],
)
def test_invalid_configuration(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Backoff(**kwargs)  # type: ignore[arg-type]


def test_attempts_are_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        Backoff().ceiling(0)
