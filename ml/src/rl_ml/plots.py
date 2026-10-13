"""Small static figures for docs/ml and MLflow (matplotlib, Agg backend, PNG <= ~100 KB).

Colours follow the reference data-viz palette: series in fixed categorical order (blue, orange,
aqua), reference lines in neutral grey, text in ink colours, recessive grid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from sklearn.metrics import precision_recall_curve

SERIES: Final = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
INK: Final = "#0b0b0b"
INK_2: Final = "#52514e"
GRID: Final = "#e4e3df"
REF: Final = "#8a8984"
SURFACE: Final = "#fcfcfb"


def _style(ax: Any, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    ax.set_xlabel(xlabel, fontsize=9, color=INK_2)
    ax.set_ylabel(ylabel, fontsize=9, color=INK_2)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)


def _save(fig: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.patch.set_facecolor(SURFACE)
    fig.tight_layout()
    fig.savefig(path, dpi=100, facecolor=SURFACE, pil_kwargs={"optimize": True})
    plt.close(fig)
    return path


def pr_curves(
    y: np.ndarray,
    scores: dict[str, np.ndarray],
    base_rate: float,
    path: Path,
    title: str = "Precision-recall on the test set",
) -> Path:
    """One line per model (fixed colour order) + the base-rate reference."""
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for i, (name, s) in enumerate(scores.items()):
        p, r, _ = precision_recall_curve(y, s)
        uniq = np.unique(s).size
        if uniq <= 2:  # binary rule → a single operating point
            ax.plot(r[1:2], p[1:2], "o", ms=8, color=SERIES[i % 4], label=name, mec=SURFACE, mew=2)
        else:
            ax.plot(r, p, lw=2, color=SERIES[i % 4], label=name)
    ax.axhline(base_rate, color=REF, lw=1.2, ls="--", label=f"base rate {base_rate:.3f}")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    _style(ax, title, "Recall", "Precision")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper right")
    return _save(fig, path)


def reliability(bins: list[dict[str, float]], path: Path, label: str, title: str) -> Path:
    """Equal-mass reliability diagram: mean predicted vs observed failure rate."""
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    pm = [b["p_mean"] for b in bins]
    obs = [b["observed_rate"] for b in bins]
    hi = max(max(pm), max(obs), 0.05) * 1.08
    ax.plot([0, hi], [0, hi], color=REF, lw=1.2, ls="--", label="perfect calibration")
    ax.plot(pm, obs, "-o", color=SERIES[0], lw=2, ms=6, mec=SURFACE, mew=1.5, label=label)
    ax.set_xlim(0, hi)
    ax.set_ylim(0, hi)
    _style(ax, title, "Mean predicted probability (10 equal-mass bins)", "Observed failure rate")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper left")
    return _save(fig, path)


def importance(
    rows: list[dict[str, Any]],
    path: Path,
    top: int = 15,
    title: str = "Global importance (mean |SHAP|)",
) -> Path:
    rows = rows[:top][::-1]
    fig, ax = plt.subplots(figsize=(8.0, 0.28 * len(rows) + 1.2))
    names = [f"{r['label']} ({r['feature']})" for r in rows]
    ax.barh(names, [r["mean_abs_shap"] for r in rows], color=SERIES[0], height=0.65)
    _style(ax, "", "mean |SHAP| (log-odds)", "")
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)
    ax.grid(True, axis="x", color=GRID)
    ax.grid(False, axis="y")
    return _save(fig, path)
