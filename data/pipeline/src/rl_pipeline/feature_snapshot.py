"""Feature snapshot for ``ml/mart_build_features`` (the dbt Python model's logic, ADR-0006).

The dbt-duckdb Python model only wires relations: it converts ``mart_build_inputs`` and
``split_cutpoints`` to Polars (:func:`to_polars`), calls :func:`build_feature_snapshot` and hands
the result back as an Arrow stream (:func:`arrow_stream`). All feature formulas come from
``rl_core.features.offline.compute_features``; nothing here computes a feature.

Rules:

* Features are computed over **every** build of ``mart_build_inputs`` (labelled or not): rl_core
  never counts a build without an outcome (``failed`` NULL) or without a finish time as history,
  but every build's start counts for ``hours_since_last_build``. Rows without a label are dropped
  afterwards (ADR-0005: no observed outcome, excluded from ML).
* ``split`` follows the global chronological cut points (``split_cutpoints``): ``train`` before
  ``validation_start_at``, ``validation`` before ``test_start_at``, ``test`` from there on.
* The feature schema version in the output path (dbt var) must equal the installed rl_core's
  ``FEATURE_SCHEMA_VERSION``; a mismatch raises instead of mislabelling the snapshot.
* Deterministic: rows sorted by ``build_id``; rl_core's computation is order-independent and
  vectorised (numpy), so identical inputs give identical values.
"""

from __future__ import annotations

from typing import Any, Final

import polars as pl

from rl_core.features.change import BUILD_INPUT_FIELDS
from rl_core.features.offline import AUDIT_COLUMN, REQUIRED_COLUMNS, compute_features
from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FORBIDDEN_INPUTS

ID_COLUMNS: Final = ("build_id", "project", "started_at", "label", "split")
SPLITS: Final = ("train", "validation", "test")
OUTPUT_COLUMNS: Final = (*ID_COLUMNS, *ALL_FEATURE_NAMES, AUDIT_COLUMN)


class FeatureSchemaMismatchError(RuntimeError):
    """The snapshot would be written under a schema version rl_core does not implement."""


class _ArrowStream:
    """Exposes a Polars frame through the Arrow PyCapsule stream protocol only.

    DuckDB's ``register``/replacement scans accept any object with ``__arrow_c_stream__``; passing
    the Polars frame itself makes DuckDB call ``DataFrame.to_arrow()``, which needs pyarrow.
    """

    __slots__ = ("_frame",)

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame

    def __arrow_c_stream__(self, requested_schema: object | None = None) -> object:
        return self._frame.__arrow_c_stream__(requested_schema)


def arrow_stream(frame: pl.DataFrame) -> Any:
    """Wrap ``frame`` for DuckDB without pyarrow (see :class:`_ArrowStream`)."""
    return _ArrowStream(frame)


def to_polars(relation: Any) -> pl.DataFrame:
    """A DuckDB relation (or anything with ``__arrow_c_stream__``) as a Polars frame, no pyarrow."""
    return pl.DataFrame(relation)


def _cut(cutpoints: pl.DataFrame, name: str) -> Any:
    if cutpoints.height != 1:
        raise ValueError(f"split_cutpoints must have exactly one row, got {cutpoints.height}")
    return cutpoints[name][0]


def assign_split(started_at: pl.Expr, validation_start_at: Any, test_start_at: Any) -> pl.Expr:
    """``train`` / ``validation`` / ``test`` by timestamp; a NULL cut means "no later split"."""
    expr: pl.Expr = pl.lit("train")
    if validation_start_at is not None:
        expr = pl.when(started_at >= pl.lit(validation_start_at)).then(pl.lit("validation"))
        expr = expr.otherwise(pl.lit("train"))
    if test_start_at is not None:
        expr = pl.when(started_at >= pl.lit(test_start_at)).then(pl.lit("test")).otherwise(expr)
    return expr


def build_feature_snapshot(
    inputs: pl.DataFrame,
    cutpoints: pl.DataFrame,
    *,
    expected_schema_version: str = FEATURE_SCHEMA_VERSION,
) -> pl.DataFrame:
    """rl_core features of every labelled build + ids, label and chronological split."""
    if expected_schema_version != FEATURE_SCHEMA_VERSION:
        raise FeatureSchemaMismatchError(
            f"dbt var feature_schema_version={expected_schema_version!r} but rl_core implements "
            f"{FEATURE_SCHEMA_VERSION!r}: pass the installed version (rl_pipeline.dbt does)"
        )
    missing = [c for c in (*REQUIRED_COLUMNS, "label", *BUILD_INPUT_FIELDS) if c not in inputs]
    if missing:
        raise ValueError(f"mart_build_inputs lacks columns: {missing}")
    leaked = sorted(set(ALL_FEATURE_NAMES) & FORBIDDEN_INPUTS)
    if leaked:  # guards the schema itself: a forbidden input can never become a feature
        raise ValueError(f"forbidden inputs among the feature names: {leaked}")

    features = compute_features(inputs.select(*REQUIRED_COLUMNS, *BUILD_INPUT_FIELDS))
    labelled = inputs.filter(pl.col("label").is_not_null()).select(
        "build_id", "project", "started_at", pl.col("label").cast(pl.Int32)
    )
    snapshot = labelled.join(features, on="build_id", how="left", validate="1:1").with_columns(
        assign_split(
            pl.col("started_at"),
            _cut(cutpoints, "validation_start_at"),
            _cut(cutpoints, "test_start_at"),
        ).alias("split")
    )
    return snapshot.select(OUTPUT_COLUMNS).sort("build_id")
