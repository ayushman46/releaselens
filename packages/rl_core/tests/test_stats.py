import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rl_core.stats import (
    bootstrap_quantile_diff,
    chi_square,
    mann_whitney,
    two_proportion_ztest,
    wilson_interval,
)

# ----------------------------------------------------------------------------- wilson


def test_wilson_known_value() -> None:
    # Reference: Newcombe (1998) / statsmodels proportion_confint(81, 263, method="wilson")
    ci = wilson_interval(81, 263)
    assert ci.estimate == pytest.approx(81 / 263)
    assert ci.low == pytest.approx(0.2553, abs=1e-4)
    assert ci.high == pytest.approx(0.3662, abs=1e-4)


@given(n=st.integers(1, 5000), frac=st.floats(0, 1))
def test_wilson_bounds_contain_estimate(n: int, frac: float) -> None:
    k = round(frac * n)
    ci = wilson_interval(k, n)
    assert 0.0 <= ci.low <= ci.estimate <= ci.high <= 1.0


def test_wilson_extremes_are_not_degenerate() -> None:
    zero = wilson_interval(0, 50)
    assert zero.low == 0.0
    assert zero.high > 0.0
    full = wilson_interval(50, 50)
    assert full.high == 1.0
    assert full.low < 1.0


@pytest.mark.parametrize(("k", "n"), [(-1, 10), (11, 10), (0, 0)])
def test_wilson_rejects_invalid(k: int, n: int) -> None:
    with pytest.raises(ValueError):
        wilson_interval(k, n)


# ----------------------------------------------------------------------------- z-test


def test_ztest_detects_clear_increase() -> None:
    res = two_proportion_ztest(10, 2000, 80, 2000)  # 0.5% -> 4%
    assert res.diff == pytest.approx(0.035)
    assert res.z > 0
    assert res.p_value < 1e-10


def test_ztest_one_sided_ignores_decrease() -> None:
    res = two_proportion_ztest(80, 2000, 10, 2000)
    assert res.p_value > 0.99


def test_ztest_matches_reference_two_sided() -> None:
    # Hand derivation: p1=0.06, p2=0.09, pooled=75/1000=0.075,
    # se = sqrt(0.075*0.925*(1/500+1/500)) = 0.0166583, z = 0.03/se = 1.80090
    res = two_proportion_ztest(30, 500, 45, 500, alternative="two-sided")
    se = math.sqrt(0.075 * 0.925 * (2 / 500))
    assert res.z == pytest.approx(0.03 / se, rel=1e-12)
    assert res.z == pytest.approx(1.80090, abs=1e-5)
    assert res.p_value == pytest.approx(0.071717, abs=1e-5)


def test_ztest_zero_variance_is_not_significant() -> None:
    res = two_proportion_ztest(0, 300, 0, 300)
    assert res.p_value == 1.0
    assert res.z == 0.0


# ----------------------------------------------------------------------------- bootstrap


def test_bootstrap_is_deterministic_for_a_seed() -> None:
    rng = np.random.default_rng(1)
    base, post = rng.normal(100, 10, 500), rng.normal(130, 10, 500)
    a = bootstrap_quantile_diff(base, post, 0.95, seed=42)
    b = bootstrap_quantile_diff(base, post, 0.95, seed=42)
    assert a == b
    assert bootstrap_quantile_diff(base, post, 0.95, seed=43) != a


def test_bootstrap_detects_real_shift_and_not_null() -> None:
    rng = np.random.default_rng(7)
    base = rng.lognormal(4.6, 0.3, 1500)  # ~100 ms median
    shifted = base + 250.0  # deterministic +250 ms (the demo regression shape)
    res = bootstrap_quantile_diff(base, shifted, 0.95, seed=0)
    assert res.diff == pytest.approx(250.0, rel=1e-6)
    assert res.excludes_zero()
    assert res.low > 200

    same = bootstrap_quantile_diff(base, rng.permutation(base), 0.95, seed=0)
    assert not same.excludes_zero()


@settings(max_examples=30, deadline=None)
@given(
    base=st.lists(st.floats(0, 1e4), min_size=1, max_size=60),
    post=st.lists(st.floats(0, 1e4), min_size=1, max_size=60),
    q=st.sampled_from([0.5, 0.95, 0.99]),
)
def test_bootstrap_interval_is_ordered(base: list[float], post: list[float], q: float) -> None:
    res = bootstrap_quantile_diff(base, post, q, n_resamples=50, seed=3)
    assert res.low <= res.high
    assert math.isfinite(res.low)
    assert math.isfinite(res.high)


def test_bootstrap_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        bootstrap_quantile_diff([], [1.0], 0.5)
    with pytest.raises(ValueError):
        bootstrap_quantile_diff([1.0], [1.0], 1.0)


# ----------------------------------------------------------------------------- mann-whitney


def test_cliffs_delta_extremes_and_symmetry() -> None:
    hi, lo = [10, 11, 12, 13], [1, 2, 3]
    assert mann_whitney(hi, lo).cliffs_delta == pytest.approx(1.0)
    assert mann_whitney(lo, hi).cliffs_delta == pytest.approx(-1.0)
    assert mann_whitney([1, 2, 3], [1, 2, 3]).cliffs_delta == pytest.approx(0.0)


@settings(max_examples=40, deadline=None)
@given(
    x=st.lists(st.integers(0, 20), min_size=1, max_size=25),
    y=st.lists(st.integers(0, 20), min_size=1, max_size=25),
)
def test_cliffs_delta_matches_pairwise_definition(x: list[int], y: list[int]) -> None:
    gt = sum(1 for a in x for b in y if a > b)
    lt = sum(1 for a in x for b in y if a < b)
    expected = (gt - lt) / (len(x) * len(y))
    assert mann_whitney(x, y).cliffs_delta == pytest.approx(expected, abs=1e-9)


def test_magnitude_labels() -> None:
    assert mann_whitney([1, 2, 3], [1, 2, 3]).magnitude == "negligible"
    assert mann_whitney([10, 11], [1, 2]).magnitude == "large"


# ----------------------------------------------------------------------------- chi-square


def test_chi_square_and_cramers_v_reference() -> None:
    # 2x2 with strong association; scipy chi2_contingency(correction=False) reference
    res = chi_square([[90, 10], [30, 70]])
    assert res.dof == 1
    assert res.n == 200
    assert res.chi2 == pytest.approx(75.0, rel=1e-6)
    assert res.cramers_v == pytest.approx(math.sqrt(75.0 / 200), rel=1e-6)
    assert res.p_value < 1e-15


def test_chi_square_drops_empty_margins_and_validates() -> None:
    res = chi_square([[5, 5, 0], [5, 5, 0]])
    assert res.cramers_v == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ValueError):
        chi_square([[1, 2, 3]])
