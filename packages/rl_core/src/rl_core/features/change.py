"""Change / test / codebase / process / time features — vectorised, NaN for "unknown".

Inputs use the canonical *build input* names (``BUILD_INPUT_FIELDS``). The offline pipeline maps
TravisTorrent's 2017 column names onto them in dbt (``ml/mart_build_inputs``); the online path
maps the GitHub Action payload onto them. Booleans are encoded 1.0/0.0.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]

BUILD_INPUT_FIELDS: Final = (
    "src_churn",
    "test_churn",
    "files_added",
    "files_deleted",
    "files_modified",
    "tests_added",
    "tests_deleted",
    "src_files",
    "doc_files",
    "other_files",
    "sloc",
    "test_lines_per_kloc",
    "test_cases_per_kloc",
    "asserts_per_kloc",
    "num_commits_in_push",
    "num_all_built_commits",
    "team_size",
    "by_core_team_member",
    "is_pr",
    "description_complexity",
    "commits_on_files_touched",
    "repo_age_days",
)

_SECONDS_PER_DAY = 86_400.0
_HOURS_PER_WEEK = 168.0


def _col(inputs: Mapping[str, npt.ArrayLike], name: str, n: int) -> F64:
    value = inputs.get(name)
    if value is None:
        return np.full(n, np.nan)
    arr = np.asarray(value, dtype=np.float64).reshape(-1)
    if arr.size == 1 and n != 1:
        arr = np.full(n, arr[0])
    if arr.size != n:
        raise ValueError(f"input {name!r} has {arr.size} values, expected {n}")
    return arr


def _nonneg(x: F64) -> F64:
    """Negative counts are data errors → unknown (NaN), never silently clipped."""
    return np.where(x < 0, np.nan, x)


def change_features(inputs: Mapping[str, npt.ArrayLike], n: int) -> dict[str, F64]:
    c = {name: _col(inputs, name, n) for name in BUILD_INPUT_FIELDS}
    src, test = _nonneg(c["src_churn"]), _nonneg(c["test_churn"])
    total = src + test
    added, deleted, modified = (
        _nonneg(c["files_added"]),
        _nonneg(c["files_deleted"]),
        _nonneg(c["files_modified"]),
    )
    t_add, t_del = _nonneg(c["tests_added"]), _nonneg(c["tests_deleted"])

    touches = np.where(
        np.isnan(test) & np.isnan(t_add) & np.isnan(t_del),
        np.nan,
        ((np.nan_to_num(test) > 0) | (np.nan_to_num(t_add) + np.nan_to_num(t_del) > 0)).astype(
            np.float64
        ),
    )
    return {
        "src_churn": src,
        "test_churn": test,
        "total_churn": total,
        "log1p_src_churn": np.log1p(src),
        "log1p_test_churn": np.log1p(test),
        "log1p_total_churn": np.log1p(total),
        "files_added": added,
        "files_deleted": deleted,
        "files_modified": modified,
        "files_touched": added + deleted + modified,
        "src_files": _nonneg(c["src_files"]),
        "doc_files": _nonneg(c["doc_files"]),
        "other_files": _nonneg(c["other_files"]),
        "tests_added": t_add,
        "tests_deleted": t_del,
        "test_churn_ratio": test / (total + 1.0),
        "touches_tests": touches,
        "log1p_sloc": np.log1p(_nonneg(c["sloc"])),
        "test_lines_per_kloc": _nonneg(c["test_lines_per_kloc"]),
        "test_cases_per_kloc": _nonneg(c["test_cases_per_kloc"]),
        "asserts_per_kloc": _nonneg(c["asserts_per_kloc"]),
        "num_commits_in_push": _nonneg(c["num_commits_in_push"]),
        "num_all_built_commits": _nonneg(c["num_all_built_commits"]),
        "team_size": _nonneg(c["team_size"]),
        "by_core_team_member": c["by_core_team_member"],
        "is_pr": c["is_pr"],
        "description_complexity": _nonneg(c["description_complexity"]),
        "commits_on_files_touched": _nonneg(c["commits_on_files_touched"]),
        "project_age_days": _nonneg(c["repo_age_days"]),
    }


def time_features(started_at_epoch_s: npt.ArrayLike) -> dict[str, F64]:
    """Cyclical hour-of-week (UTC). Monday 00:00 → 0. 1970-01-01 was a Thursday (index 3)."""
    t = np.asarray(started_at_epoch_s, dtype=np.float64)
    days = np.floor(t / _SECONDS_PER_DAY)
    weekday = np.mod(days + 3.0, 7.0)
    hour = np.floor(np.mod(t, _SECONDS_PER_DAY) / 3600.0)
    how = weekday * 24.0 + hour
    angle = 2.0 * np.pi * how / _HOURS_PER_WEEK
    return {"hour_of_week_sin": np.sin(angle), "hour_of_week_cos": np.cos(angle)}
