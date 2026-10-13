"""Effect-size helpers against textbook formulas computed by hand (no shared code)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from rl_analysis.effects import (
    chi_square_test,
    holm,
    magnitude_label,
    mantel_haenszel,
    odds_ratio,
    quantiles,
    rate,
    risk_difference,
    risk_ratio,
)

Z = 1.959963984540054


def test_rate_is_wilson_and_handles_empty(hand: Any) -> None:
    r = rate(3, 10)
    low, high = hand.wilson(3, 10)
    assert r["rate"] == pytest.approx(0.3)
    assert (r["ci95_low"], r["ci95_high"]) == (pytest.approx(low), pytest.approx(high))
    # textbook value for 3/10: 0.1078 - 0.6032
    assert r["ci95_low"] == pytest.approx(0.10779, abs=1e-5)
    assert r["ci95_high"] == pytest.approx(0.60322, abs=1e-5)
    assert rate(0, 0) == {"n": 0, "failures": 0, "rate": None, "ci95_low": None, "ci95_high": None}
    assert rate(0, 4)["ci95_low"] == 0.0
    assert rate(4, 4)["ci95_high"] == 1.0


def test_risk_difference_is_newcombe_hybrid_score(hand: Any) -> None:
    # 2/10 vs 5/6 (the fixture's core-team table)
    d = risk_difference(2, 10, 5, 6)
    assert d is not None
    l1, u1 = hand.wilson(2, 10)
    l2, u2 = hand.wilson(5, 6)
    p1, p2 = 0.2, 5 / 6
    assert d["value"] == pytest.approx(p1 - p2)
    assert d["ci95_low"] == pytest.approx(p1 - p2 - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2))
    assert d["ci95_high"] == pytest.approx(p1 - p2 + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2))
    assert risk_difference(1, 0, 1, 2) is None


def test_risk_ratio_is_katz_log() -> None:
    rr = risk_ratio(2, 10, 5, 6)
    assert rr is not None
    assert rr["value"] == pytest.approx(0.24)
    se = math.sqrt(1 / 2 - 1 / 10 + 1 / 5 - 1 / 6)  # = 0.658281
    assert se == pytest.approx(0.658281, abs=1e-6)
    assert rr["ci95_low"] == pytest.approx(math.exp(math.log(0.24) - Z * se))
    assert rr["ci95_high"] == pytest.approx(math.exp(math.log(0.24) + Z * se))
    assert rr["ci95_low"] == pytest.approx(0.066051, abs=1e-6)
    assert risk_ratio(0, 10, 5, 6) is None


def test_odds_ratio_is_woolf() -> None:
    o = odds_ratio(2, 10, 5, 6)  # [[2, 8], [5, 1]] -> OR = 2*1 / (8*5) = 0.05
    assert o is not None
    assert o["value"] == pytest.approx(0.05)
    se = math.sqrt(1 / 2 + 1 / 8 + 1 / 5 + 1 / 1)
    assert o["ci95_low"] == pytest.approx(math.exp(math.log(0.05) - Z * se))
    assert o["ci95_high"] == pytest.approx(math.exp(math.log(0.05) + Z * se))
    assert odds_ratio(0, 10, 5, 6) is None


def test_chi_square_2x2_closed_form(hand: Any) -> None:
    # groups (key, n, failures): core 10/2, non-core 6/5 -> table [[2, 8], [5, 1]]
    t = chi_square_test("k", "f", "d", [("core", 10, 2), ("non_core", 6, 5)])
    expected = 16 * (2 * 1 - 8 * 5) ** 2 / (10 * 6 * 7 * 9)  # n (ad - bc)^2 / margins
    assert expected == pytest.approx(6.112169, abs=1e-6)
    assert t["status"] == "ok"
    assert t["chi2"] == pytest.approx(expected)
    assert t["dof"] == 1
    assert t["cramers_v"] == pytest.approx(38 / math.sqrt(10 * 6 * 7 * 9))
    assert t["p_value"] == pytest.approx(hand.chi2_sf(expected, 1))
    assert t["magnitude"] == "large"  # V = 0.618
    # smallest expected count: 6 * 7 / 16 = 2.625
    assert t["min_expected_count"] == pytest.approx(2.625)


def test_chi_square_not_computable_cases() -> None:
    one_group = chi_square_test("a", "f", "d", [("x", 4, 2), ("y", 0, 0)])
    assert one_group["status"] == "not_computable"
    no_failures = chi_square_test("b", "f", "d", [("x", 3, 0), ("y", 1, 0)])
    assert no_failures["status"] == "not_computable"
    assert no_failures["n"] == 4
    assert no_failures["groups"] == ["x", "y"]


def test_magnitude_labels_follow_cohen() -> None:
    assert magnitude_label(0.099, 1) == "negligible"
    assert magnitude_label(0.1, 1) == "small"
    assert magnitude_label(0.3, 1) == "medium"
    assert magnitude_label(0.5, 1) == "large"
    # df* = 4: w = V * 2, so V = 0.05 is already "small"
    assert magnitude_label(0.05, 4) == "small"


def test_holm_step_down_by_hand() -> None:
    p = [0.01, 0.04, 0.03, 0.005]
    # sorted: 0.005*4 = 0.02, 0.01*3 = 0.03, 0.03*2 = 0.06, 0.04*1 = 0.04 -> cummax 0.06
    assert holm(p) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert holm([0.6, 0.9]) == pytest.approx([1.0, 1.0])
    assert holm([]) == []


def test_mantel_haenszel_by_hand(hand: Any) -> None:
    # two strata (project A, project B) of core (1) vs non-core (2) from the fixture
    strata = [("A", 1, 5, 3, 4), ("B", 1, 5, 2, 2), ("C", 1, 3, 0, 0)]
    mh = mantel_haenszel("k", "f", "d", strata)
    assert mh["status"] == "ok"
    assert mh["strata"] == 2
    assert mh["strata_dropped"] == 1  # C has no second group
    assert mh["n"] == 16
    # OR_MH = sum(a d / n) / sum(b c / n): A a=1 b=4 c=3 d=1 n=9; B a=1 b=4 c=2 d=0 n=7
    or_mh = (1 * 1 / 9 + 1 * 0 / 7) / (4 * 3 / 9 + 4 * 2 / 7)
    assert or_mh == pytest.approx(0.044872, abs=1e-6)
    assert mh["pooled_odds_ratio"] == pytest.approx(or_mh)
    # CMH = (sum(a - E a))^2 / sum Var a, E a = (a+b)(a+c)/n, Var = (a+b)(c+d)(a+c)(b+d)/(n^2(n-1))
    ea = 5 * 4 / 9 + 5 * 3 / 7
    var = 5 * 4 * 4 * 5 / (81 * 8) + 5 * 2 * 3 * 4 / (49 * 6)
    cmh = (2 - ea) ** 2 / var
    assert mh["cmh_statistic"] == pytest.approx(cmh)
    assert mh["p_value"] == pytest.approx(hand.chi2_sf(cmh, 1))
    assert mh["ci95_low"] < or_mh < mh["ci95_high"]


def test_mantel_haenszel_without_information() -> None:
    mh = mantel_haenszel("k", "f", "d", [("A", 0, 3, 0, 2)])
    assert mh["status"] == "not_computable"


def test_quantiles_are_linear_interpolation() -> None:
    values = np.asarray(
        [0, 0, 0, 5, 7, 10, 20, 50, 90, 100, 200, 400, 700, 1500, 3000, 6000], dtype=np.float64
    )
    # h = (n - 1) q: 3.75 -> 5 + 0.75 * 2; 7.5 -> 50 + 0.5 * 40; 11.25 -> 400 + 0.25 * 300
    assert quantiles(values, (0.25, 0.5, 0.75)) == [6.5, 70.0, 475.0]
