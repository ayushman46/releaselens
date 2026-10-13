"""Effect sizes and interval helpers on top of :mod:`rl_core.stats` (which stays the source of
the Wilson interval, the chi-square test with Cramér's V and Mann-Whitney with Cliff's delta).

* :func:`rate` - a proportion with its 95% Wilson interval (``rl_core.stats.wilson_interval``).
* :func:`risk_difference` - ``p1 - p2`` with Newcombe's hybrid score interval (method 10,
  Newcombe 1998), built from the two Wilson intervals.
* :func:`risk_ratio` - ``p1 / p2`` with the Katz log interval.
* :func:`chi_square_test` - Pearson chi-square (``rl_core.stats.chi_square``) of a groups x
  {failed, passed} table, plus the smallest expected cell count and Cohen's magnitude label.
* :func:`odds_ratio` / :func:`mantel_haenszel` - crude and within-project (stratified) odds
  ratios (``statsmodels.stats.contingency_tables``).
* :func:`holm` - Holm step-down adjusted p-values (``statsmodels.stats.multitest``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Final

import numpy as np
from scipy.stats import chi2 as _chi2
from statsmodels.stats.contingency_tables import StratifiedTable, Table2x2
from statsmodels.stats.multitest import multipletests

from rl_core.stats import chi_square, wilson_interval

CONFIDENCE: Final = 0.95
Z95: Final = 1.959963984540054  # scipy.stats.norm.ppf(0.975)
# Cohen (1988) thresholds for w = V * sqrt(min(r, c) - 1): 0.1 small, 0.3 medium, 0.5 large.
_W_THRESHOLDS: Final = ((0.5, "large"), (0.3, "medium"), (0.1, "small"))


def rate(failures: int, n: int) -> dict[str, Any]:
    """``failures / n`` with its 95% Wilson interval; ``None`` fields when ``n == 0``."""
    if n <= 0:
        return {"n": n, "failures": failures, "rate": None, "ci95_low": None, "ci95_high": None}
    ci = wilson_interval(failures, n, CONFIDENCE)
    return {
        "n": n,
        "failures": failures,
        "rate": ci.estimate,
        "ci95_low": ci.low,
        "ci95_high": ci.high,
    }


def risk_difference(x1: int, n1: int, x2: int, n2: int) -> dict[str, Any] | None:
    """``p1 - p2`` with Newcombe's hybrid score 95% interval; ``None`` if a group is empty."""
    if n1 <= 0 or n2 <= 0:
        return None
    a = wilson_interval(x1, n1, CONFIDENCE)
    b = wilson_interval(x2, n2, CONFIDENCE)
    d = a.estimate - b.estimate
    low = d - math.sqrt((a.estimate - a.low) ** 2 + (b.high - b.estimate) ** 2)
    high = d + math.sqrt((a.high - a.estimate) ** 2 + (b.estimate - b.low) ** 2)
    return {"value": d, "ci95_low": low, "ci95_high": high, "method": "newcombe_hybrid_score"}


def risk_ratio(x1: int, n1: int, x2: int, n2: int) -> dict[str, Any] | None:
    """``p1 / p2`` with the Katz log 95% interval; ``None`` if undefined (an empty group or a
    zero count in either group)."""
    if n1 <= 0 or n2 <= 0 or x1 <= 0 or x2 <= 0:
        return None
    rr = (x1 / n1) / (x2 / n2)
    se = math.sqrt(1 / x1 - 1 / n1 + 1 / x2 - 1 / n2)
    return {
        "value": rr,
        "ci95_low": math.exp(math.log(rr) - Z95 * se),
        "ci95_high": math.exp(math.log(rr) + Z95 * se),
        "method": "katz_log",
    }


def magnitude_label(cramers_v: float, df_star: int) -> str:
    """Cohen's label for Cramér's V via w = V * sqrt(df*), df* = min(rows, cols) - 1."""
    w = cramers_v * math.sqrt(max(df_star, 1))
    for threshold, label in _W_THRESHOLDS:
        if w >= threshold:
            return label
    return "negligible"


def chi_square_test(
    key: str,
    finding: str,
    description: str,
    groups: Sequence[tuple[str, int, int]],
) -> dict[str, Any]:
    """Pearson chi-square of independence for ``groups`` x {failed, passed}.

    ``groups`` holds ``(group key, n, failures)``. Empty groups are dropped (as
    ``rl_core.stats.chi_square`` drops all-zero rows); a table without two non-empty groups and
    both outcomes is reported as ``not_computable`` instead of raising.
    """
    used = [(g, n, f) for g, n, f in groups if n > 0]
    n_total = sum(n for _, n, _ in used)
    record: dict[str, Any] = {
        "key": key,
        "finding": finding,
        "description": description,
        "groups": [g for g, _, _ in used],
        "n": n_total,
    }
    table = np.array([[f, n - f] for _, n, f in used], dtype=np.float64).reshape(-1, 2)
    try:
        res = chi_square(table)
    except ValueError as exc:
        return {**record, "status": "not_computable", "reason": str(exc)}
    kept = table[table.sum(axis=1) > 0][:, table.sum(axis=0) > 0]
    expected = np.outer(kept.sum(axis=1), kept.sum(axis=0)) / kept.sum()
    df_star = min(kept.shape) - 1
    return {
        **record,
        "status": "ok",
        "table_shape": [int(kept.shape[0]), int(kept.shape[1])],
        "chi2": res.chi2,
        "dof": res.dof,
        "p_value": res.p_value,
        "cramers_v": res.cramers_v,
        "magnitude": magnitude_label(res.cramers_v, df_star),
        "min_expected_count": float(expected.min()),
    }


def odds_ratio(x1: int, n1: int, x2: int, n2: int) -> dict[str, Any] | None:
    """Crude odds ratio of group 1 vs group 2 with the Woolf (log) 95% interval
    (``statsmodels`` ``Table2x2``); ``None`` when a cell is zero."""
    cells = (x1, n1 - x1, x2, n2 - x2)
    if min(cells) <= 0:
        return None
    table = Table2x2(np.asarray([[x1, n1 - x1], [x2, n2 - x2]], dtype=np.float64))
    low, high = table.oddsratio_confint(alpha=1 - CONFIDENCE)
    return {
        "value": float(table.oddsratio),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "method": "woolf_log",
    }


def mantel_haenszel(
    key: str,
    finding: str,
    description: str,
    strata: Sequence[tuple[str, int, int, int, int]],
) -> dict[str, Any]:
    """Association of group 1 vs group 2 with failure **within** strata (here: projects).

    ``strata`` holds ``(stratum, failures_1, n_1, failures_2, n_2)``. Strata in which a group is
    empty carry no within-stratum information and are dropped (counted). Returns the
    Mantel-Haenszel pooled odds ratio with its 95% interval (Robins-Breslow-Greenland variance)
    and the Cochran-Mantel-Haenszel test (no continuity correction), via ``statsmodels``
    ``StratifiedTable``.
    """
    used = [(s, f1, n1, f2, n2) for s, f1, n1, f2, n2 in strata if n1 > 0 and n2 > 0]
    record: dict[str, Any] = {
        "key": key,
        "finding": finding,
        "description": description,
        "strata": len(used),
        "strata_dropped": len(strata) - len(used),
        "n": sum(n1 + n2 for _, _, n1, _, n2 in used),
    }
    tables = [
        np.asarray([[f1, n1 - f1], [f2, n2 - f2]], dtype=np.float64) for _, f1, n1, f2, n2 in used
    ]
    numerator = sum(t[0, 0] * t[1, 1] / t.sum() for t in tables)
    denominator = sum(t[0, 1] * t[1, 0] / t.sum() for t in tables)
    if not tables or numerator <= 0 or denominator <= 0:
        return {**record, "status": "not_computable", "reason": "no informative stratum"}
    st = StratifiedTable(tables)
    low, high = st.oddsratio_pooled_confint(alpha=1 - CONFIDENCE)
    statistic = float(st.test_null_odds(correction=False).statistic)
    return {
        **record,
        "status": "ok",
        "pooled_odds_ratio": float(st.oddsratio_pooled),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "cmh_statistic": statistic,
        # chi2.sf instead of statsmodels' 1 - cdf: keeps precision for small p-values
        "p_value": float(_chi2.sf(statistic, 1)),
        "method": "mantel_haenszel",
    }


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values, in input order (family-wise error control)."""
    if not p_values:
        return []
    adjusted = multipletests(np.asarray(p_values, dtype=np.float64), method="holm")[1]
    return [float(p) for p in adjusted]


def quantiles(values: np.ndarray[Any, Any], qs: Sequence[float]) -> list[float]:
    """Linear-interpolation quantiles (numpy's default; DuckDB's ``quantile_cont``)."""
    return [float(v) for v in np.quantile(values, list(qs))]
