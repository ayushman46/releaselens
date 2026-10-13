"""In-process latency of one risk assessment (no network, no database).

    python -m rl_api.risk.bench --model <bundle dir | models:/…> [--tracking-uri URI]
        [--calls 500] [--history 200] [--nthread 1]

Times what the API does per request once the history rows are loaded: ``compute_online``
(change facts + a 200-run history) and scoring with explanations (``bundle.assess`` with the
full SHAP vector). Prints JSON with p50/p95/p99 in milliseconds and the hardware.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from rl_api.risk import service
from rl_api.risk.modelstore import load_model
from rl_core.features.online import PastRun, bounded_history, compute_online

FACTS: dict[str, Any] = {
    "src_churn": 120,
    "test_churn": 40,
    "files_added": 1,
    "files_deleted": 0,
    "files_modified": 6,
    "tests_added": 2,
    "tests_deleted": 0,
    "src_files": 5,
    "doc_files": 1,
    "other_files": 1,
    "sloc": 48211,
    "test_lines_per_kloc": 410.2,
    "test_cases_per_kloc": 21.5,
    "asserts_per_kloc": 55.0,
    "num_all_built_commits": 3,
    "team_size": 9,
    "by_core_team_member": True,
    "is_pr": True,
    "commits_on_files_touched": 14,
    "repo_age_days": 1203,
}


def _history(n: int, now: datetime, seed: int = 7) -> list[PastRun]:
    rng = np.random.default_rng(seed)
    runs = []
    for i in range(n):
        start = now - timedelta(hours=2 * (n - i))
        dur = float(rng.integers(120, 1800))
        runs.append(
            PastRun(
                started_at=start,
                finished_at=start + timedelta(seconds=dur),
                failed=bool(rng.random() < 0.25),
                duration_s=dur,
            )
        )
    return runs


def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values), q))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rl_api.risk.bench")
    p.add_argument("--model", required=True)
    p.add_argument("--tracking-uri", default=os.environ.get("RL_MLFLOW_TRACKING_URI"))
    p.add_argument("--cache-dir", default="~/.cache/releaselens/models")
    p.add_argument("--calls", type=int, default=500)
    p.add_argument("--warmup", type=int, default=20)
    p.add_argument("--history", type=int, default=200)
    p.add_argument("--nthread", type=int, default=1, help="XGBoost threads (the API uses 1)")
    args = p.parse_args(argv)

    model = load_model(
        "main", args.model, tracking_uri=args.tracking_uri, cache_dir=Path(args.cache_dir)
    )
    est = getattr(model.bundle.model, "estimator", None)
    if est is not None and hasattr(est, "get_booster"):
        est.set_params(n_jobs=args.nthread)
        est.get_booster().set_param({"nthread": args.nthread})
    now = datetime.now(UTC)
    full = _history(args.history, now)
    window, totals = bounded_history(full, now, max(args.history, 20))

    def once() -> tuple[float, float]:
        t0 = time.perf_counter()
        features = compute_online(FACTS, window, now, totals=totals)
        t1 = time.perf_counter()
        service.score(model, features)
        t2 = time.perf_counter()
        return (t1 - t0) * 1e3, (t2 - t0) * 1e3

    for _ in range(args.warmup):
        once()
    feats_ms, total_ms = zip(*(once() for _ in range(args.calls)), strict=True)
    out = {
        "model_version": model.version,
        "calls": args.calls,
        "history_runs": args.history,
        "xgboost_nthread": args.nthread,
        "features_ms": {"p50": _pct(list(feats_ms), 50), "p95": _pct(list(feats_ms), 95)},
        "total_ms": {
            "p50": _pct(list(total_ms), 50),
            "p95": _pct(list(total_ms), 95),
            "p99": _pct(list(total_ms), 99),
            "mean": statistics.fmean(total_ms),
        },
        "hardware": {
            "machine": platform.machine(),
            "processor": platform.processor(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
    }
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
