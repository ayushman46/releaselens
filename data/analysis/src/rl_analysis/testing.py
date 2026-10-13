"""SYNTHETIC marts fixture for the analytics tests. Never real data.

16 hand-made builds in two invented projects (plus one canceled build), laid out exactly like a dbt
run's published outputs (``<root>/marts/<model>/run_id=<id>/part-00000.parquet``,
``<root>/features/release_features/schema=<v>/run_id=<id>/...`` + ``_manifest.json``) with the
real column types, so :mod:`rl_analysis.inputs` reads it the same way as the real run. Every
expected value in ``tests/`` is derived by hand from :data:`SYNTHETIC_BUILDS`.

``prev_build_failed`` (the leakage-safe feature) is hand-derived from the timeline with rl_core's
rule (most recent labelled build that FINISHED strictly before the start, finish = start +
duration); ``tests/test_fixture.py`` re-derives it with ``rl_core.features.history``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Final

import duckdb
import polars as pl

from rl_analysis.findings import DBT_CHURN_LABELS

SYNTHETIC_DATASET: Final = "synthetic-analytics-fixture"
SYNTHETIC_VERSION: Final = "synthetic0000000000000000000000"  # not an md5: marks the fixture
FEATURE_SCHEMA_VERSION: Final = "1.0.0"

# (build_id, project, started_at UTC, duration_s, status, src_churn, test_churn, is_pr,
#  by_core_team_member, prev_build_failed [leakage-safe; None = no finished previous build])
SYNTHETIC_BUILDS: Final = (
    (101, "synthetic/alpha", "2015-01-05 10:00:00", 600, "passed", 0, 0, False, True, None),
    (102, "synthetic/alpha", "2015-01-05 12:00:00", 1200, "failed", 50, 10, False, True, 0.0),
    (103, "synthetic/alpha", "2015-01-05 12:10:00", 300, "failed", 200, 0, True, False, 0.0),
    (104, "synthetic/alpha", "2015-01-06 09:00:00", 900, "passed", 10, 90, False, True, 1.0),
    (105, "synthetic/alpha", "2015-01-06 11:00:00", 1800, "errored", 700, 100, False, False, 0.0),
    (106, "synthetic/alpha", "2015-01-06 11:05:00", 600, "passed", 3000, 0, True, False, 0.0),
    (107, "synthetic/alpha", "2015-02-02 08:00:00", 400, "passed", 5, 0, False, True, 1.0),
    (108, "synthetic/alpha", "2015-02-02 10:00:00", 2400, "failed", 6000, 500, False, False, 0.0),
    (109, "synthetic/alpha", "2015-02-03 10:00:00", 500, "passed", 20, 20, True, True, 1.0),
    (201, "synthetic/beta", "2015-01-07 10:00:00", 300, "passed", 0, 40, False, True, None),
    (202, "synthetic/beta", "2015-01-07 11:00:00", 600, "failed", 400, 0, False, True, 0.0),
    (203, "synthetic/beta", "2015-01-07 11:05:00", 60, "canceled", 30, 0, False, True, None),
    (204, "synthetic/beta", "2015-01-07 13:00:00", 1500, "failed", 90, 10, True, False, 1.0),
    (205, "synthetic/beta", "2015-02-04 09:00:00", 700, "passed", 1500, 300, False, True, 1.0),
    (206, "synthetic/beta", "2015-02-04 09:10:00", 800, "passed", 0, 0, False, True, 1.0),
    (207, "synthetic/beta", "2015-02-05 09:00:00", 1000, "failed", 7, 3, False, False, 0.0),
    (208, "synthetic/beta", "2015-02-05 12:00:00", 450, "passed", 100, 0, False, True, 1.0),
)

# Hand-derived NAIVE links (int_build_history): build_id -> (naive_prev_build_id,
# naive_prev_label, naive_prev_finished_before_start).
SYNTHETIC_NAIVE: Final[dict[int, tuple[int | None, int | None, bool | None]]] = {
    101: (None, None, None),
    102: (101, 0, True),
    103: (102, 1, False),
    104: (103, 1, True),
    105: (104, 0, True),
    106: (105, 1, False),
    107: (106, 0, True),
    108: (107, 0, True),
    109: (108, 1, True),
    201: (None, None, None),
    202: (201, 0, True),
    203: (202, 1, False),
    204: (203, None, True),
    205: (204, 1, True),
    206: (205, 0, False),
    207: (206, 0, True),
    208: (207, 1, True),
}

_LABEL = {"passed": 0, "failed": 1, "errored": 1}
_UTC = pl.Datetime("us", "UTC")


def _bucket(total: int) -> str:
    """dbt's churn bucket label (int_change_profile)."""
    for label, upper in zip(DBT_CHURN_LABELS, (100, 500, 1000, 5000), strict=False):
        if total <= upper:
            return label
    return DBT_CHURN_LABELS[-1]


def _ts(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


def builds_frame() -> pl.DataFrame:
    """``fct_builds`` columns used by the analysis (real types)."""
    rows = []
    for bid, project, start, dur, status, src, test, pr, core, _prev in SYNTHETIC_BUILDS:
        started = _ts(start)
        rows.append(
            {
                "build_id": bid,
                "project": project,
                "language": "java",
                "started_at": started,
                "started_month": started.date().replace(day=1),
                "finished_at_upper": started + dt.timedelta(seconds=dur),
                "status": status,
                "label": _LABEL.get(status),
                "duration_s": dur,
                "is_pr": pr,
                "by_core_team_member": core,
                "src_churn": src,
                "test_churn": test,
                "total_churn": src + test,
                "churn_bucket": _bucket(src + test),
            }
        )
    return pl.DataFrame(
        rows,
        schema={
            "build_id": pl.Int64,
            "project": pl.Utf8,
            "language": pl.Utf8,
            "started_at": _UTC,
            "started_month": pl.Date,
            "finished_at_upper": _UTC,
            "status": pl.Utf8,
            "label": pl.Int32,
            "duration_s": pl.Int32,
            "is_pr": pl.Boolean,
            "by_core_team_member": pl.Boolean,
            "src_churn": pl.Int64,
            "test_churn": pl.Int64,
            "total_churn": pl.Int64,
            "churn_bucket": pl.Utf8,
        },
    )


def features_frame() -> pl.DataFrame:
    """``mart_build_features``: labelled builds only, the columns the analysis reads."""
    rows = [
        {
            "build_id": bid,
            "project": project,
            "started_at": _ts(start),
            "label": _LABEL[status],
            "split": "train",
            "prev_build_failed": prev,
        }
        for bid, project, start, _d, status, *_rest, prev in SYNTHETIC_BUILDS
        if status in _LABEL
    ]
    return pl.DataFrame(
        rows,
        schema={
            "build_id": pl.Int64,
            "project": pl.Utf8,
            "started_at": _UTC,
            "label": pl.Int32,
            "split": pl.Utf8,
            "prev_build_failed": pl.Float64,
        },
    )


_BUCKET_ORDER = {label: i for i, label in enumerate(DBT_CHURN_LABELS, start=1)}


def _marts_from_builds(builds: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``mart_change_risk`` (churn dimension) and ``mart_build_reliability``, as dbt counts them."""
    failures = (pl.col("label") == 1).sum().cast(pl.Int64).alias("n_failures")
    change = (
        builds.group_by("churn_bucket")
        .agg(
            pl.len().cast(pl.Int64).alias("n_builds"),
            pl.col("label").count().cast(pl.Int64).alias("n_labelled_builds"),
            failures,
        )
        .rename({"churn_bucket": "bucket"})
        .with_columns(
            pl.lit("churn_bucket").alias("dimension"),
            pl.col("bucket")
            .replace_strict(_BUCKET_ORDER, return_dtype=pl.Int32)
            .alias("bucket_order"),
        )
        .select(
            "dimension", "bucket", "bucket_order", "n_builds", "n_labelled_builds", "n_failures"
        )
        .sort("bucket_order")
    )
    reliability = (
        builds.group_by("project", "started_month")
        .agg(
            pl.len().cast(pl.Int64).alias("n_builds"),
            pl.col("label").count().cast(pl.Int64).alias("n_labelled_builds"),
            failures,
        )
        .rename({"started_month": "month_start"})
        .sort("project", "month_start")
    )
    return change, reliability


def write_synthetic_run(
    root: Path,
    run_id: str = "synthetic",
    *,
    builds: pl.DataFrame | None = None,
    change_risk: pl.DataFrame | None = None,
) -> Path:
    """Write the SYNTHETIC run under ``root`` (a dbt-like output root); return ``root``.

    ``builds`` / ``change_risk`` override the generated frames (tests use this to tamper with
    one mart and expect the cross-checks to fail).
    """
    builds = builds if builds is not None else builds_frame()
    change, reliability = _marts_from_builds(builds)
    if change_risk is not None:
        change = change_risk
    marts = {
        "fct_builds": builds,
        "mart_change_risk": change,
        "mart_build_reliability": reliability,
    }
    for model, frame in marts.items():
        path = root / "marts" / model / f"run_id={run_id}" / "part-00000.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(path, compression="zstd")
    fdir = root / "features" / "release_features" / f"schema={FEATURE_SCHEMA_VERSION}"
    fdir = fdir / f"run_id={run_id}"
    fdir.mkdir(parents=True, exist_ok=True)
    features = features_frame()
    fpath = fdir / "part-00000.parquet"
    features.write_parquet(fpath, compression="zstd")
    manifest = {
        "dataset": SYNTHETIC_DATASET,
        "dataset_version": SYNTHETIC_VERSION,
        "run_id": run_id,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "file": {
            "path": "part-00000.parquet",
            "rows": features.height,
            "bytes": fpath.stat().st_size,
            "sha256": hashlib.sha256(fpath.read_bytes()).hexdigest(),
        },
        "label_rate": None,
        "cutpoints": None,
    }
    (fdir / "_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return root


def write_synthetic_dbt_duckdb(
    path: Path, naive: dict[int, tuple[int | None, int | None, bool | None]] | None = None
) -> Path:
    """A DuckDB file holding a hand-typed ``int_build_history`` (the naive-link check's target)."""
    links = naive if naive is not None else SYNTHETIC_NAIVE
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE OR REPLACE TABLE int_build_history (build_id BIGINT, "
            "naive_prev_build_id BIGINT, naive_prev_label INTEGER, "
            "naive_prev_finished_before_start BOOLEAN)"
        )
        con.executemany(
            "INSERT INTO int_build_history VALUES (?, ?, ?, ?)",
            [[bid, *link] for bid, link in sorted(links.items())],
        )
    finally:
        con.close()
    return path
