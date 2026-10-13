"""Shared fixtures: the SYNTHETIC marts run of :mod:`rl_analysis.testing` (never real data)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from rl_analysis.findings import AnalysisConfig
from rl_analysis.report import ReportRequest, build_report
from rl_analysis.settings import AnalysisSettings
from rl_analysis.testing import write_synthetic_dbt_duckdb, write_synthetic_run

Z = 1.959963984540054
# Thresholds scaled to the 16-build fixture (the real report uses 200 / 100 / 10).
SMALL = AnalysisConfig(min_project_builds=3, min_month_builds=7, top_n=1, bootstrap_resamples=200)


@pytest.fixture
def synthetic_root(tmp_path: Path) -> Path:
    return write_synthetic_run(tmp_path / "out")


@pytest.fixture
def synthetic_duckdb(tmp_path: Path) -> Path:
    return write_synthetic_dbt_duckdb(tmp_path / "releaselens.duckdb")


@pytest.fixture
def make_report(synthetic_root: Path) -> Callable[..., dict[str, Any]]:
    def _make(config: AnalysisConfig = SMALL, **kwargs: Any) -> dict[str, Any]:
        request = ReportRequest(
            run_id="synthetic", input_root=str(synthetic_root), config=config, **kwargs
        )
        return build_report(request, AnalysisSettings())

    return _make


@pytest.fixture
def report(make_report: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    return make_report()


# ------------------------------------------------------------------ independent "by hand" math
class Hand:
    """Textbook formulas, independent of rl_core / scipy / statsmodels (exposed as ``hand``)."""

    @staticmethod
    def wilson(x: int, n: int) -> tuple[float, float]:
        return wilson_by_hand(x, n)

    @staticmethod
    def chi2(groups: Sequence[tuple[int, int]]) -> float:
        return chi2_by_hand(groups)

    @staticmethod
    def chi2_sf(x: float, k: int) -> float:
        return chi2_sf_by_hand(x, k)


@pytest.fixture
def hand() -> type[Hand]:
    return Hand


@pytest.fixture
def get_finding() -> Callable[[dict[str, Any], str], dict[str, Any]]:
    def _get(report: dict[str, Any], key: str) -> dict[str, Any]:
        return next(f for f in report["findings"] if f["key"] == key)

    return _get


@pytest.fixture
def get_test() -> Callable[[dict[str, Any], str], dict[str, Any]]:
    def _get(report: dict[str, Any], key: str) -> dict[str, Any]:
        return next(t for t in report["chi_square_tests"]["tests"] if t["key"] == key)

    return _get


def wilson_by_hand(x: int, n: int) -> tuple[float, float]:
    p = x / n
    denom = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denom
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denom
    low = 0.0 if x == 0 else centre - half
    high = 1.0 if x == n else centre + half
    return low, high


def chi2_by_hand(groups: Sequence[tuple[int, int]]) -> float:
    """Pearson chi-square of (n, failures) groups x {failed, passed}: sum (O - E)^2 / E."""
    groups = [(n, f) for n, f in groups if n > 0]
    total = sum(n for n, _ in groups)
    fails = sum(f for _, f in groups)
    stat = 0.0
    for n, f in groups:
        for observed, col in ((f, fails), (n - f, total - fails)):
            expected = n * col / total
            stat += (observed - expected) ** 2 / expected
    return stat


def chi2_sf_by_hand(x: float, k: int) -> float:
    """Closed-form chi-square survival function for integer dof (no scipy)."""
    if k % 2 == 0:
        term, acc = 1.0, 1.0
        for i in range(1, k // 2):
            term *= (x / 2) / i
            acc += term
        return math.exp(-x / 2) * acc
    acc = math.erfc(math.sqrt(x / 2))
    term = math.sqrt(2 * x / math.pi) * math.exp(-x / 2)
    for j in range(1, (k - 1) // 2 + 1):
        acc += term
        term *= x / (2 * j + 1)
    return acc
