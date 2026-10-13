"""Every computation of the report on the SYNTHETIC fixture, against hand-derived values.

The counts below were derived by hand from ``rl_analysis.testing.SYNTHETIC_BUILDS`` (16 labelled
builds in two invented projects, plus one canceled build); statistics use the textbook formulas
in ``conftest.py`` (independent of rl_core / scipy / statsmodels).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator
from typing import Any

import duckdb
import pytest

from rl_analysis.findings import Analysis, AnalysisConfig

Report = dict[str, Any]
Get = Callable[[Report, str], dict[str, Any]]


def _rates(series: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    return [(s["key"], s["n"], s["failures"]) for s in series]


def test_population(report: Report) -> None:
    pop = report["population"]
    assert pop["builds"] == 17
    assert pop["status_counts"] == {"canceled": 1, "errored": 1, "failed": 6, "passed": 9}
    assert pop["labelled_builds"] == 16
    assert pop["excluded_without_outcome"] == 1
    assert pop["projects"] == 2
    assert pop["failure"]["failures"] == 7
    assert pop["failure"]["rate"] == pytest.approx(7 / 16)
    assert pop["first_started_at"] == "2015-01-05T10:00:00Z"
    assert pop["last_started_at"] == "2015-02-05T12:00:00Z"
    assert report["dataset"]["version"] == "synthetic0000000000000000000000"


def test_checks_pass_on_consistent_marts(report: Report) -> None:
    assert set(report["checks"]["cross_checks"].values()) == {0}
    assert report["checks"]["naive_link_vs_int_build_history"]["status"] == "not_run"


def test_change_size(report: Report, get_finding: Get, get_test: Get, hand: Any) -> None:
    f = get_finding(report, "change_size")
    groups = [(10, 3), (2, 2), (1, 1), (2, 0), (1, 1)]
    assert _rates(f["series"]["buckets"]) == [
        ("0-100", 10, 3),
        ("101-500", 2, 2),
        ("501-1000", 1, 1),
        ("1001-5000", 2, 0),
        ("5001+", 1, 1),
    ]
    first = f["series"]["buckets"][0]
    assert first["rate"] == pytest.approx(0.3)
    low, high = hand.wilson(3, 10)
    assert first["ci95_low"] == pytest.approx(low)
    assert first["ci95_high"] == pytest.approx(high)
    t = get_test(report, "change_size")
    # by hand: sum (O - E)^2 / E with E = n * 7/16 and n * 9/16  ->  112 / 15
    assert hand.chi2(groups) == pytest.approx(112 / 15)
    assert t["chi2"] == pytest.approx(112 / 15)
    assert t["dof"] == 4
    assert t["cramers_v"] == pytest.approx(math.sqrt(112 / 15 / 16))
    assert t["p_value"] == pytest.approx(hand.chi2_sf(112 / 15, 4))
    # largest vs smallest bucket: 1/1 - 3/10
    assert f["effects"]["risk_difference"]["value"] == pytest.approx(0.7)
    assert f["effects"]["risk_ratio"]["value"] == pytest.approx(1 / 0.3)
    assert f["effects"]["monotonic_increase"] is False  # 1001-5000 has rate 0
    assert f["headline"]["effect_value"] == pytest.approx(t["cramers_v"])


def test_src_test_churn(report: Report, get_finding: Get, get_test: Get, hand: Any) -> None:
    f = get_finding(report, "src_test_churn")
    cut = f["src_churn_cutpoints"]
    assert (cut["q25"], cut["q50"], cut["q75"]) == (6.5, 70.0, 475.0)
    assert cut["share_zero"] == pytest.approx(3 / 16)
    cells = {c["key"]: (c["n"], c["failures"]) for c in f["series"]["cells"]}
    assert cells == {
        "Q1|no_test_change": (3, 0),
        "Q1|tests_changed": (1, 0),
        "Q2|no_test_change": (0, 0),
        "Q2|tests_changed": (4, 2),
        "Q3|no_test_change": (3, 2),
        "Q3|tests_changed": (1, 1),
        "Q4|no_test_change": (1, 0),
        "Q4|tests_changed": (3, 2),
    }
    assert _rates(f["series"]["by_quartile"]) == [
        ("Q1", 4, 0),
        ("Q2", 4, 2),
        ("Q3", 4, 3),
        ("Q4", 4, 2),
    ]
    assert _rates(f["series"]["by_tests_changed"]) == [
        ("no_test_change", 7, 2),
        ("tests_changed", 9, 5),
    ]
    # test-change ratio, boundaries exact: 10/100 -> (0, 0.1], 3/10 -> (0.1, 0.3]
    assert _rates(f["series"]["test_change_ratio"]) == [
        ("no_line_churn", 2, 0),
        ("0", 5, 2),
        ("0-0.1", 2, 2),
        ("0.1-0.3", 4, 3),
        ("0.3-1", 3, 0),
    ]
    cells_t = get_test(report, "src_quartile_x_tests_changed")
    expected = hand.chi2(list(cells.values()))  # the empty Q2 cell is dropped
    assert cells_t["chi2"] == pytest.approx(expected)
    assert cells_t["dof"] == 6
    assert cells_t["p_value"] == pytest.approx(hand.chi2_sf(expected, 6))
    quart = get_test(report, "src_churn_quartile")
    q_expected = hand.chi2([(4, 0), (4, 2), (4, 3), (4, 2)])
    assert quart["chi2"] == pytest.approx(q_expected)
    assert quart["p_value"] == pytest.approx(hand.chi2_sf(q_expected, 3))
    tests_t = get_test(report, "tests_changed")
    assert tests_t["chi2"] == pytest.approx(16 * (2 * 4 - 5 * 5) ** 2 / (7 * 9 * 7 * 9))
    assert tests_t["cramers_v"] == pytest.approx(17 / 63)
    # within quartiles: Q1 has no failures, Q2 no "no test change" group
    assert get_test(report, "tests_changed_within_q1")["status"] == "not_computable"
    assert get_test(report, "tests_changed_within_q2")["status"] == "not_computable"
    assert get_test(report, "tests_changed_within_q3")["chi2"] == pytest.approx(4 / 9)
    assert get_test(report, "tests_changed_within_q4")["chi2"] == pytest.approx(16 / 12)
    within = {w["quartile"]: w["risk_difference"] for w in f["effects"]["within_quartile"]}
    assert within["Q2"] is None
    assert within["Q3"]["value"] == pytest.approx(1 - 2 / 3)
    ratio_t = get_test(report, "test_change_ratio")
    assert ratio_t["n"] == 14
    assert ratio_t["chi2"] == pytest.approx(hand.chi2([(5, 2), (2, 2), (4, 3), (3, 0)]))
    assert f["effects"]["tests_changed_vs_not"]["value"] == pytest.approx(5 / 9 - 2 / 7)


def test_previous_build_leakage_safe_and_naive(
    report: Report, get_finding: Get, get_test: Get, hand: Any
) -> None:
    f = get_finding(report, "previous_build")
    s, n = f["leakage_safe"], f["naive"]
    assert (s["p_fail_given_prev_failed"]["n"], s["p_fail_given_prev_failed"]["failures"]) == (7, 1)
    assert (s["p_fail_given_prev_passed"]["n"], s["p_fail_given_prev_passed"]["failures"]) == (7, 6)
    assert s["risk_difference"]["value"] == pytest.approx(1 / 7 - 6 / 7)
    assert get_test(report, "prev_build_leakage_safe")["chi2"] == pytest.approx(50 / 7)
    assert get_test(report, "prev_build_leakage_safe")["cramers_v"] == pytest.approx(35 / 49)
    assert (n["p_fail_given_prev_failed"]["n"], n["p_fail_given_prev_failed"]["failures"]) == (6, 1)
    assert (n["p_fail_given_prev_passed"]["n"], n["p_fail_given_prev_passed"]["failures"]) == (7, 5)
    naive_chi2 = 13 * (1 * 2 - 5 * 5) ** 2 / (6 * 7 * 6 * 7)
    assert get_test(report, "prev_build_naive")["chi2"] == pytest.approx(naive_chi2)
    assert hand.chi2([(6, 1), (7, 5)]) == pytest.approx(naive_chi2)
    assert f["population"]["excluded"] == {
        "leakage_safe_no_finished_previous_build": 2,  # 101, 201
        "naive_first_build_of_project": 2,  # 101, 201
        "naive_previous_build_without_outcome": 1,  # 204 (predecessor 203 canceled)
    }
    assert n["pairs"] == 13
    assert n["overlapping_pairs"] == 3  # 103, 106, 206
    ov, fin = n["by_overlap"]["overlapping"], n["by_overlap"]["finished_before_start"]
    assert ov["n"] == 3
    assert (ov["p_fail_given_prev_failed"]["n"], ov["p_fail_given_prev_failed"]["failures"]) == (
        2,
        1,
    )
    assert (ov["p_fail_given_prev_passed"]["n"], ov["p_fail_given_prev_passed"]["failures"]) == (
        1,
        0,
    )
    assert fin["risk_difference"]["value"] == pytest.approx(0 / 4 - 5 / 6)
    assert n["by_overlap"]["unknown_finish"]["n"] == 0
    # 103, 106, 107, 206 differ between the two links (107: naive 106 passed, but 105 failed
    # and finished later, so it is the most recent FINISHED build)
    assert f["agreement"] == {
        "compared": 13,
        "same_previous_outcome": 9,
        "different_previous_outcome": 4,
    }
    assert f["headline"]["effect_name"] == "risk_difference"
    assert f["headline"]["effect_value"] == pytest.approx(-5 / 7)


def test_duration(report: Report, get_finding: Get, hand: Any) -> None:
    f = get_finding(report, "duration")
    p, x = f["groups"]["passed"], f["groups"]["failed"]
    # passed 300 400 450 500 600 600 700 800 900; failed 300 600 1000 1200 1500 1800 2400
    assert (p["n"], p["median_s"], p["q25_s"], p["q75_s"], p["iqr_s"]) == (9, 600, 450, 700, 250)
    assert (x["n"], x["median_s"], x["q25_s"], x["q75_s"], x["iqr_s"]) == (7, 1200, 800, 1650, 850)
    by_status = {s["key"]: (s["n"], s["median_s"]) for s in f["series"]["by_status"]}
    assert by_status == {"errored": (1, 1800), "failed": (6, 1100), "passed": (9, 600)}
    mw = f["mann_whitney"]
    # U(failed) = #(failed > passed) + 0.5 #ties = 0.5 + 5 + 9 * 5
    assert mw["u"] == 50.5
    assert mw["cliffs_delta"] == pytest.approx(2 * 50.5 / 63 - 1)  # = 38/63
    assert mw["magnitude"] == "large"
    # normal approximation with tie correction and continuity correction
    sigma = math.sqrt(63 / 12 * (17 - (6 + 24) / (16 * 15)))
    z = (50.5 - 31.5 - 0.5) / sigma
    assert mw["p_value"] == pytest.approx(math.erfc(z / math.sqrt(2)), rel=1e-9)
    assert f["effects"]["median_difference_s"]["value"] == 600
    assert f["population"]["excluded"] == {"no_duration": 0}


def test_projects(report: Report, get_finding: Get, get_test: Get) -> None:
    f = get_finding(report, "projects")
    assert _rates(f["series"]["top"]) == [("synthetic/alpha", 9, 4)]
    assert _rates(f["series"]["bottom"]) == [("synthetic/beta", 7, 3)]
    t = get_test(report, "projects")
    assert t["chi2"] == pytest.approx(16 * (4 * 4 - 5 * 3) ** 2 / (9 * 7 * 7 * 9))
    assert f["population"]["projects"] == 2
    assert f["always_failing"] == []
    assert f["rate_distribution"]["median"] == pytest.approx((4 / 9 + 3 / 7) / 2)


def test_projects_threshold_excludes_small_projects(
    make_report: Callable[..., Report], get_finding: Get
) -> None:
    r = make_report(
        AnalysisConfig(min_project_builds=8, min_month_builds=7, top_n=1, bootstrap_resamples=50)
    )
    f = get_finding(r, "projects")
    assert f["population"]["projects"] == 1
    assert f["population"]["excluded"] == {
        "projects_below_threshold": 1,
        "builds_of_projects_below_threshold": 7,
    }
    assert (
        next(t for t in r["chi_square_tests"]["tests"] if t["key"] == "projects")["status"]
        == "not_computable"
    )


def test_monthly_trend(report: Report, get_finding: Get, get_test: Get) -> None:
    f = get_finding(report, "monthly_trend")
    months = [
        (m["key"], m["n"], m["failures"], m["projects"], m["low_volume"])
        for m in f["series"]["months"]
    ]
    assert months == [("2015-01", 9, 5, 2, False), ("2015-02", 7, 2, 2, False)]
    assert get_test(report, "months")["chi2"] == pytest.approx(16 * 17**2 / (9 * 7 * 7 * 9))


def test_monthly_low_volume_flag(make_report: Callable[..., Report], get_finding: Get) -> None:
    r = make_report(
        AnalysisConfig(min_project_builds=3, min_month_builds=8, top_n=1, bootstrap_resamples=50)
    )
    f = get_finding(r, "monthly_trend")
    assert [m["low_volume"] for m in f["series"]["months"]] == [False, True]
    assert f["population"]["low_volume_months"] == 1
    assert (
        next(t for t in r["chi_square_tests"]["tests"] if t["key"] == "months")["status"]
        == "not_computable"
    )


def test_core_team_and_pull_requests(
    report: Report, get_finding: Get, get_test: Get, hand: Any
) -> None:
    core = get_finding(report, "core_team")
    assert _rates(core["series"]["groups"]) == [("core_team", 10, 2), ("non_core", 6, 5)]
    assert get_test(report, "core_team")["chi2"] == pytest.approx(23104 / 3780)
    assert core["effects"]["risk_difference"]["value"] == pytest.approx(0.2 - 5 / 6)
    assert core["effects"]["risk_ratio"]["value"] == pytest.approx(0.24)
    mh = core["effects"]["within_projects"]
    assert mh["pooled_odds_ratio"] == pytest.approx((1 / 9) / (12 / 9 + 8 / 7))
    assert mh["crude_odds_ratio"]["value"] == pytest.approx((2 * 1) / (8 * 5))
    # push & non-core: 105, 108, 207 (all failed); push & core: 101 102 104 107 201 202 205 206
    # 208 (102, 202 failed); PR & non-core: 103, 106, 204 (103, 204 failed); PR & core: 109
    assert [(c["key"], c["n"], c["failures"]) for c in core["series"]["by_pull_request"]] == [
        ("push|non_core", 3, 3),
        ("push|core_team", 9, 2),
        ("pull_request|non_core", 3, 2),
        ("pull_request|core_team", 1, 0),
    ]
    pr = get_finding(report, "pull_requests")
    assert _rates(pr["series"]["groups"]) == [("pull_request", 4, 2), ("push", 12, 5)]
    assert get_test(report, "pull_requests")["chi2"] == pytest.approx(256 / 3024)
    assert hand.chi2([(4, 2), (12, 5)]) == pytest.approx(256 / 3024)


def test_holm_family(report: Report) -> None:
    tests = report["chi_square_tests"]["tests"]
    ok = [t for t in tests if t["status"] == "ok"]
    assert report["chi_square_tests"]["computed"] == len(ok) == 13
    assert report["chi_square_tests"]["not_computable"] == 2
    # Holm by hand: sort ascending, multiply by (m - rank), running max, cap at 1
    m = len(ok)
    order = sorted(range(m), key=lambda i: ok[i]["p_value"])
    adjusted, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * ok[i]["p_value"]))
        adjusted[i] = running
    for t, expected in zip(ok, adjusted, strict=True):
        assert t["p_value_holm"] == pytest.approx(expected, rel=1e-9)
    for f in report["findings"]:
        key = f["headline"]["test_key"]
        if key is not None:
            rec = next(t for t in tests if t["key"] == key)
            assert f["headline"]["p_value_holm"] == rec.get("p_value_holm")
    strat = report["stratified_tests"]["tests"]
    assert [t["key"] for t in strat] == [
        "tests_changed_within_projects",
        "prev_build_leakage_safe_within_projects",
        "prev_build_naive_within_projects",
        "core_team_within_projects",
        "pull_requests_within_projects",
    ]


def test_every_finding_states_n_effect_test_and_caveat(report: Report) -> None:
    for f in report["findings"]:
        assert f["population"]["n"] > 0
        h = f["headline"]
        assert h["effect_name"] in {"cramers_v", "risk_difference", "cliffs_delta"}
        assert h["test"] in {"pearson_chi_square", "mann_whitney_u"}
        assert h["p_value"] is not None
        assert any("not causation" in c for c in f["caveats"])
        assert f["summary"]


def test_within_projects_crude_odds_ratio_uses_the_same_builds() -> None:
    # A and B have both groups; C has group FALSE only (all failed) and is dropped by MH. The
    # crude OR next to the MH OR must be computed on A + B only, else dropping C moves it too.
    sql = (
        "SELECT * FROM (VALUES ('A', true, 4, 2), ('A', false, 6, 1), ('B', true, 5, 4), "
        "('B', false, 5, 1), ('C', false, 10, 10)) AS t(project, grp, n, failures)"
    )
    with duckdb.connect() as con:
        rec = Analysis(con).within_projects("k", "f", "d", sql)
    assert (rec["strata"], rec["strata_dropped"], rec["n"]) == (2, 1, 20)
    # MH: A ad/n = 2*5/10, bc/n = 2*1/10; B ad/n = 4*4/10, bc/n = 1*1/10
    assert rec["pooled_odds_ratio"] == pytest.approx((1.0 + 1.6) / (0.2 + 0.1))
    # same builds: 6 of 9 vs 2 of 11 failed -> (6 * 9) / (3 * 2)
    assert rec["crude_odds_ratio"]["value"] == pytest.approx(9.0)
    # all builds: 6 of 9 vs 12 of 21 failed -> (6 * 9) / (3 * 12)
    assert rec["crude_odds_ratio_all_projects"]["value"] == pytest.approx(1.5)


_CAUSAL = re.compile(
    r"\b(causes?|caused|causing|leads? to|led to|drives?|driven by|because of|due to|"
    r"results? in)\b",
    re.IGNORECASE,
)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def test_no_causal_wording(report: Report) -> None:
    from rl_analysis.markdown import render_markdown

    texts = [*_strings(report), render_markdown(report, generated_on="2015-03-01")]
    assert [m.group(0) for t in texts for m in _CAUSAL.finditer(t)] == []
