"""feature_snapshot: rl_core features + label filter + chronological split (no dbt needed).

All rows are SYNTHETIC, built inline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import polars as pl
import pytest

from rl_core.features.change import BUILD_INPUT_FIELDS
from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from rl_pipeline.feature_snapshot import (
    OUTPUT_COLUMNS,
    FeatureSchemaMismatchError,
    arrow_stream,
    build_feature_snapshot,
    to_polars,
)

T0 = datetime(2014, 3, 1, 12, 0, tzinfo=UTC)


def _inputs(rows: list[dict[str, Any]]) -> pl.DataFrame:
    base = dict.fromkeys(BUILD_INPUT_FIELDS, 1.0)
    frame = pl.DataFrame([{**base, **r} for r in rows])
    return frame.with_columns(
        pl.col("started_at").cast(pl.Datetime("us", "UTC")),
        pl.col("finished_at").cast(pl.Datetime("us", "UTC")),
    )


def _build(
    build_id: int, minutes: int, label: int | None, duration: int | None = 60
) -> dict[str, Any]:
    started = T0 + timedelta(minutes=minutes)
    return {
        "build_id": build_id,
        "project": "synthetic/p",
        "started_at": started,
        "finished_at": None if duration is None else started + timedelta(seconds=duration),
        "failed": label,
        "duration_s": None if duration is None else float(duration),
        "label": label,
    }


def _cuts(validation: datetime | None, test: datetime | None) -> pl.DataFrame:
    return pl.DataFrame(
        {"validation_start_at": [validation], "test_start_at": [test]},
        schema={
            "validation_start_at": pl.Datetime("us", "UTC"),
            "test_start_at": pl.Datetime("us", "UTC"),
        },
    )


@pytest.fixture
def inputs() -> pl.DataFrame:
    return _inputs(
        [
            _build(5, 0, 0),
            _build(3, 10, 1),
            _build(4, 20, None),  # canceled: no label, but its start still counts
            _build(1, 30, 0),
            _build(2, 40, 1, duration=None),  # errored without duration: never history
            _build(6, 50, 0),
        ]
    )


def test_labelled_rows_only_sorted_with_every_feature(inputs: pl.DataFrame) -> None:
    out = build_feature_snapshot(inputs, _cuts(T0 + timedelta(minutes=30), None))
    assert tuple(out.columns) == OUTPUT_COLUMNS
    assert out["build_id"].to_list() == [1, 2, 3, 5, 6]  # build 4 (no label) dropped
    assert set(ALL_FEATURE_NAMES) <= set(out.columns)
    assert out.schema["label"] == pl.Int32


def test_history_uses_finished_builds_only(inputs: pl.DataFrame) -> None:
    out = build_feature_snapshot(inputs, _cuts(None, None)).sort("started_at")
    by_id = {row["build_id"]: row for row in out.iter_rows(named=True)}
    # build 1 starts at +30 min: builds 5 and 3 finished (60 s each), 4 has no label
    assert by_id[1]["history_size"] == 2.0
    assert by_id[1]["prev_build_failed"] == 1.0
    # ... but the canceled build 4 (+20 min) is the most recent START: 10 minutes earlier
    assert by_id[1]["hours_since_last_build"] == pytest.approx(10 / 60)
    # build 6: build 2 never finished (no duration) so it never counts
    assert by_id[6]["history_size"] == 3.0
    for row in by_id.values():
        audit = row["max_history_input_finished_at"]
        assert audit is None or audit < row["started_at"].timestamp()


def test_split_follows_cut_timestamps(inputs: pl.DataFrame) -> None:
    cuts = _cuts(T0 + timedelta(minutes=30), T0 + timedelta(minutes=50))
    out = build_feature_snapshot(inputs, cuts)
    split = dict(zip(out["build_id"].to_list(), out["split"].to_list(), strict=True))
    assert split == {5: "train", 3: "train", 1: "validation", 2: "validation", 6: "test"}


def test_missing_cut_points_mean_no_later_split(inputs: pl.DataFrame) -> None:
    assert set(build_feature_snapshot(inputs, _cuts(None, None))["split"]) == {"train"}
    out = build_feature_snapshot(inputs, _cuts(T0 + timedelta(minutes=45), None))
    assert out.filter(pl.col("split") == "validation")["build_id"].to_list() == [6]


def test_cutpoints_must_be_one_row(inputs: pl.DataFrame) -> None:
    two = pl.concat([_cuts(None, None), _cuts(None, None)])
    with pytest.raises(ValueError, match="exactly one row"):
        build_feature_snapshot(inputs, two)


def test_schema_version_mismatch_is_refused(inputs: pl.DataFrame) -> None:
    with pytest.raises(FeatureSchemaMismatchError, match=FEATURE_SCHEMA_VERSION):
        build_feature_snapshot(inputs, _cuts(None, None), expected_schema_version="0.0.0")


def test_missing_input_column_is_refused(inputs: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="sloc"):
        build_feature_snapshot(inputs.drop("sloc"), _cuts(None, None))


def test_identical_inputs_give_identical_output(inputs: pl.DataFrame) -> None:
    cuts = _cuts(T0 + timedelta(minutes=30), T0 + timedelta(minutes=50))
    shuffled = inputs.sample(fraction=1.0, shuffle=True, seed=7)
    assert build_feature_snapshot(inputs, cuts).equals(build_feature_snapshot(shuffled, cuts))


def test_duckdb_round_trip_without_pyarrow(inputs: pl.DataFrame) -> None:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.register("snap", arrow_stream(build_feature_snapshot(inputs, _cuts(None, None))))
    back = to_polars(con.sql("SELECT * FROM snap ORDER BY build_id"))
    assert back["build_id"].to_list() == [1, 2, 3, 5, 6]
    assert back.schema["started_at"] == pl.Datetime("us", "UTC")
