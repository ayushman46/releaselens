"""Strictly-past build history features (spec §8.2, "History rule").

A past build counts for a query time ``t`` **only if it had finished before t**:
``finished_at < t`` (strict). Finish time is the true finish when known, otherwise the upper
bound ``started_at + duration`` — conservative, never optimistic (DATASET_AUDIT §4.1). Builds
with unknown finish time never count. Ordering of "previous" builds is by finish time (the order
in which results became knowable), ties broken by input order.

The same vectorised function serves offline batches (all builds of a project as queries) and
online requests (the tenant's history + one query time), so training and serving cannot drift.
Times are float seconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]

ZSCORE_WINDOW = 20
_MIN_ZSCORE_SAMPLES = 3


@dataclass(frozen=True, slots=True)
class History:
    """Past builds of ONE project. Arrays are aligned; any order is accepted."""

    finished_at: F64  # seconds; NaN = unknown → never eligible
    failed: F64  # 1.0 failed/errored, 0.0 passed, NaN = no outcome (e.g. canceled) → not eligible
    duration: F64  # seconds; NaN allowed (affects only duration features)
    started_at: F64  # seconds; used for hours_since_last_build

    @staticmethod
    def empty() -> History:
        e = np.empty(0, dtype=np.float64)
        return History(e, e, e, e)


def history_features(history: History, query_times: npt.ArrayLike) -> dict[str, F64]:
    """Compute history features for each query time against one project's history."""
    t = np.asarray(query_times, dtype=np.float64)
    n_q = t.size

    fin = np.asarray(history.finished_at, dtype=np.float64)
    fail = np.asarray(history.failed, dtype=np.float64)
    dur = np.asarray(history.duration, dtype=np.float64)
    started = np.sort(np.asarray(history.started_at, dtype=np.float64))
    started = started[~np.isnan(started)]

    keep = ~np.isnan(fin) & ~np.isnan(fail)
    fin, fail, dur = fin[keep], fail[keep], dur[keep]
    order = np.argsort(fin, kind="stable")
    fin, fail, dur = fin[order], fail[order], dur[order]
    m = fin.size

    # number of eligible builds for each query: finished strictly before t
    cnt = np.searchsorted(fin, t, side="left").astype(np.int64)

    out: dict[str, F64] = {}
    nan = np.full(n_q, np.nan)
    has = cnt > 0
    last = np.where(has, cnt - 1, 0)

    # prefix sums (index i = sum over first i eligible builds)
    cfail = np.concatenate(([0.0], np.cumsum(fail)))

    out["history_size"] = cnt.astype(np.float64)
    out["prev_build_failed"] = np.where(has, fail[last] if m else 0.0, np.nan)

    for k in (5, 20):
        lo = np.maximum(cnt - k, 0)
        n = cnt - lo
        rate = np.divide(cfail[cnt] - cfail[lo], n, out=nan.copy(), where=n > 0)
        out[f"fail_rate_last_{k}"] = rate

    # builds since last failure: index of the most recent failure at or before position i
    if m:
        idx = np.arange(m, dtype=np.float64)
        last_fail_pos = np.where(fail > 0.5, idx, -1.0)
        last_fail_pos = np.maximum.accumulate(last_fail_pos)
        lf = np.where(has, last_fail_pos[last], -1.0)
        since = np.where(lf >= 0, (cnt - 1) - lf, np.nan)
        out["builds_since_last_failure"] = np.where(has, since, np.nan)
    else:
        out["builds_since_last_failure"] = nan.copy()

    # previous build duration and its z-score vs the last 20 eligible durations
    if m:
        prev_dur = np.where(has, dur[last], np.nan)
        dz = np.nan_to_num(dur, nan=0.0)
        valid = (~np.isnan(dur)).astype(np.float64)
        c1 = np.concatenate(([0.0], np.cumsum(dz)))
        c2 = np.concatenate(([0.0], np.cumsum(dz * dz)))
        cv = np.concatenate(([0.0], np.cumsum(valid)))
        lo = np.maximum(cnt - ZSCORE_WINDOW, 0)
        nv = cv[cnt] - cv[lo]
        s1 = c1[cnt] - c1[lo]
        s2 = c2[cnt] - c2[lo]
        mean = np.divide(s1, nv, out=nan.copy(), where=nv > 0)
        var = np.divide(s2, nv, out=nan.copy(), where=nv > 0) - mean * mean
        # population variance → sample variance; guard tiny negative rounding
        var = np.where(nv > 1, np.maximum(var, 0.0) * nv / np.maximum(nv - 1, 1), np.nan)
        std = np.sqrt(var)
        ok = (nv >= _MIN_ZSCORE_SAMPLES) & (std > 1e-9) & ~np.isnan(prev_dur)
        out["prev_build_duration"] = prev_dur
        out["prev_duration_zscore_vs_project_last_20"] = np.where(
            ok, (prev_dur - mean) / np.where(ok, std, 1.0), np.nan
        )
    else:
        out["prev_build_duration"] = nan.copy()
        out["prev_duration_zscore_vs_project_last_20"] = nan.copy()

    # hours since the most recent build that STARTED strictly before t (start times are known)
    sc = np.searchsorted(started, t, side="left")
    prev_start = np.where(sc > 0, started[np.maximum(sc - 1, 0)] if started.size else 0.0, np.nan)
    out["hours_since_last_build"] = (t - prev_start) / 3600.0

    return out


def max_input_finished_at(history: History, query_times: npt.ArrayLike) -> F64:
    """Latest finish time among builds that contributed to each query (NaN if none).

    Used by leakage tests and the dbt time-ordering test: must always be < query time.
    """
    t = np.asarray(query_times, dtype=np.float64)
    fin = np.asarray(history.finished_at, dtype=np.float64)
    fail = np.asarray(history.failed, dtype=np.float64)
    fin = np.sort(fin[~np.isnan(fin) & ~np.isnan(fail)])
    cnt = np.searchsorted(fin, t, side="left")
    return np.where(cnt > 0, fin[np.maximum(cnt - 1, 0)] if fin.size else np.nan, np.nan)
