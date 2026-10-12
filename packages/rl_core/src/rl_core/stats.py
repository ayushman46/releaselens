"""Statistical primitives shared by the analytics report (M3) and the regression engine (M8).

Every function is pure and deterministic (bootstrap takes an explicit seed), and returns the
quantities needed to state *evidence*: estimate, interval, test statistic, p-value, effect size.
Nothing here decides "regressed" or "significant"; callers apply gates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from scipy import stats as _st

FloatArray = npt.NDArray[np.float64]


# ----------------------------------------------------------------------------- proportions
@dataclass(frozen=True, slots=True)
class Interval:
    estimate: float
    low: float
    high: float
    confidence: float


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> Interval:
    """Wilson score interval for a binomial proportion (well-behaved near 0/1 and small n)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be within [0, n]")
    z = float(_st.norm.ppf(1 - (1 - confidence) / 2))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # The Wilson interval always contains p̂; clamp away floating-point overshoot at p̂ ∈ {0, 1}.
    low = 0.0 if successes == 0 else min(p, max(0.0, centre - half))
    high = 1.0 if successes == n else max(p, min(1.0, centre + half))
    return Interval(p, low, high, confidence)


@dataclass(frozen=True, slots=True)
class ProportionTest:
    p1: float
    p2: float
    diff: float  # p2 - p1
    z: float
    p_value: float
    alternative: str


def two_proportion_ztest(
    x1: int,
    n1: int,
    x2: int,
    n2: int,
    alternative: Literal["larger", "two-sided"] = "larger",
) -> ProportionTest:
    """Pooled two-proportion z-test of H0: p2 == p1.

    ``alternative="larger"`` tests H1: p2 > p1 (e.g. post-deploy error rate above baseline).
    With zero pooled variance (both rates 0 or both 1) the test is undefined → z=0, p=1.
    """
    if n1 <= 0 or n2 <= 0:
        raise ValueError("sample sizes must be positive")
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0.0:
        return ProportionTest(p1, p2, p2 - p1, 0.0, 1.0, alternative)
    z = (p2 - p1) / se
    p = float(_st.norm.sf(z)) if alternative == "larger" else float(2 * _st.norm.sf(abs(z)))
    return ProportionTest(p1, p2, p2 - p1, z, p, alternative)


# ----------------------------------------------------------------------------- bootstrap
@dataclass(frozen=True, slots=True)
class BootstrapDiff:
    quantile: float
    baseline: float
    post: float
    diff: float  # post - baseline (point estimate on the observed samples)
    low: float
    high: float
    confidence: float
    n_resamples: int
    seed: int

    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0


def bootstrap_quantile_diff(
    baseline: npt.ArrayLike,
    post: npt.ArrayLike,
    quantile: float,
    *,
    n_resamples: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
    chunk: int = 200,
) -> BootstrapDiff:
    """Percentile-bootstrap CI for ``Q_q(post) - Q_q(baseline)``.

    Both samples are resampled independently with replacement ``n_resamples`` times. Uses a
    seeded ``numpy.random.Generator`` (PCG64), so identical inputs give identical intervals.
    Work is chunked to bound memory (``chunk * max(n)`` indices at a time).
    """
    a = np.asarray(baseline, dtype=np.float64)
    b = np.asarray(post, dtype=np.float64)
    if a.size == 0 or b.size == 0:
        raise ValueError("both samples must be non-empty")
    if not 0 < quantile < 1:
        raise ValueError("quantile must be in (0, 1)")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")

    rng = np.random.default_rng(seed)
    q_a = float(np.quantile(a, quantile))
    q_b = float(np.quantile(b, quantile))
    diffs = np.empty(n_resamples, dtype=np.float64)
    done = 0
    while done < n_resamples:
        k = min(chunk, n_resamples - done)
        ra = a[rng.integers(0, a.size, size=(k, a.size))]
        rb = b[rng.integers(0, b.size, size=(k, b.size))]
        diffs[done : done + k] = np.quantile(rb, quantile, axis=1) - np.quantile(
            ra, quantile, axis=1
        )
        done += k
    alpha = 1 - confidence
    low, high = np.quantile(diffs, [alpha / 2, 1 - alpha / 2])
    return BootstrapDiff(
        quantile, q_a, q_b, q_b - q_a, float(low), float(high), confidence, n_resamples, seed
    )


# ----------------------------------------------------------------------------- distributions
@dataclass(frozen=True, slots=True)
class MannWhitney:
    u: float
    p_value: float
    cliffs_delta: float  # P(X > Y) - P(X < Y), in [-1, 1]
    n_x: int
    n_y: int

    @property
    def magnitude(self) -> str:
        """Romano et al. (2006) thresholds for |delta|."""
        d = abs(self.cliffs_delta)
        if d < 0.147:
            return "negligible"
        if d < 0.33:
            return "small"
        if d < 0.474:
            return "medium"
        return "large"


def mann_whitney(x: npt.ArrayLike, y: npt.ArrayLike) -> MannWhitney:
    """Two-sided Mann-Whitney U test with Cliff's delta as the effect size.

    Cliff's delta is derived from U (ties count half): delta = 2U/(n_x n_y) - 1, which is
    O(n log n) instead of the O(n_x n_y) pairwise definition.
    """
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    if xa.size == 0 or ya.size == 0:
        raise ValueError("both samples must be non-empty")
    res = _st.mannwhitneyu(xa, ya, alternative="two-sided", method="asymptotic")
    u = float(res.statistic)
    delta = 2.0 * u / (xa.size * ya.size) - 1.0
    return MannWhitney(u, float(res.pvalue), delta, int(xa.size), int(ya.size))


# ----------------------------------------------------------------------------- association
@dataclass(frozen=True, slots=True)
class ChiSquare:
    chi2: float
    p_value: float
    dof: int
    cramers_v: float
    n: int


def chi_square(table: npt.ArrayLike) -> ChiSquare:
    """Pearson chi-square test of independence with Cramér's V (no continuity correction).

    V = sqrt(chi2 / (n * (min(r, c) - 1))). Rows/columns that are entirely zero are dropped.
    """
    t = np.asarray(table, dtype=np.float64)
    if t.ndim != 2:
        raise ValueError("table must be 2-D")
    t = t[t.sum(axis=1) > 0][:, t.sum(axis=0) > 0]
    r, c = t.shape
    if r < 2 or c < 2:
        raise ValueError("need at least a 2x2 table with non-zero margins")
    chi2, p, dof, _ = _st.chi2_contingency(t, correction=False)
    n = int(t.sum())
    v = math.sqrt(float(chi2) / (n * (min(r, c) - 1)))
    return ChiSquare(float(chi2), float(p), int(dof), v, n)
