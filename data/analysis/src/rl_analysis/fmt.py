"""Number formatting shared by the generated summaries and ``FINDINGS.md``."""

from __future__ import annotations

from typing import Any, Final

EN_DASH: Final = "\u2013"  # ranges, as in the dbt bucket labels ("0\u2013100")


def count(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{100 * value:.{digits}f}%"


def ci(low: float | None, high: float | None, digits: int = 2) -> str:
    if low is None or high is None:
        return "n/a"
    return f"{100 * low:.{digits}f}{EN_DASH}{100 * high:.{digits}f}%"


def pp(value: float | None, digits: int = 2) -> str:
    """A difference of proportions in percentage points, signed."""
    return "n/a" if value is None else f"{100 * value:+.{digits}f} pp"


def pp_ci(effect: dict[str, Any] | None, digits: int = 2) -> str:
    if not effect:
        return "n/a"
    lo, hi = effect["ci95_low"], effect["ci95_high"]
    return f"{100 * lo:+.{digits}f} to {100 * hi:+.{digits}f} pp"


def ratio(effect: dict[str, Any] | None) -> str:
    if not effect:
        return "n/a"
    lo, hi = effect["ci95_low"], effect["ci95_high"]
    return f"{effect['value']:.2f} (95% CI {lo:.2f}{EN_DASH}{hi:.2f})"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def big(value: float | None) -> str:
    """A large statistic with thousands separators (one decimal only if not integral)."""
    if value is None:
        return "n/a"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def pval(value: float | None) -> str:
    """p-values: ``0.0`` is a floating-point underflow of the tail probability, not zero."""
    if value is None:
        return "n/a"
    if value == 0.0:
        return "< 1e-300"
    if value < 1e-4:
        return f"{value:.2e}"
    return f"{value:.4f}"


def seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f} s" if float(value).is_integer() else f"{value:,.1f} s"


def bound(value: float) -> str:
    """A cut point: integer-valued floats without decimals."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def span(low: str, high: str) -> str:
    """``low`` and ``high`` joined by an en dash (U+2013)."""
    return f"{low}{EN_DASH}{high}"
