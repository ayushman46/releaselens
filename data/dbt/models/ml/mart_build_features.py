"""ml/mart_build_features: rl_core's feature vectors for every labelled build (ADR-0006).

The feature logic exists only in rl_core: this model calls
``rl_core.features.offline.compute_features`` on ``mart_build_inputs`` (ALL builds, so canceled /
started builds still count as "a build started" while never counting as finished history), keeps
the labelled builds, and adds ``split`` from ``split_cutpoints`` (global chronological cut).
The wiring lives in :mod:`rl_pipeline.feature_snapshot` (unit-tested without dbt).

Written to <output_root>/features/release_features/schema=<FEATURE_SCHEMA_VERSION>/
run_id=<run_id>/part-00000.parquet (macros/external_location.sql).
"""


def model(dbt, session):
    dbt.config(materialized="external")
    meta = dbt.config.get("meta") or {}

    from rl_pipeline.feature_snapshot import arrow_stream, build_feature_snapshot, to_polars

    inputs = to_polars(dbt.ref("mart_build_inputs"))
    cutpoints = to_polars(dbt.ref("split_cutpoints"))
    snapshot = build_feature_snapshot(
        inputs,
        cutpoints,
        expected_schema_version=str(meta.get("feature_schema_version", "")),
    )
    # dbt-duckdb registers the returned object with DuckDB; the Arrow PyCapsule stream avoids a
    # pyarrow dependency (polars and DuckDB both speak it natively).
    return arrow_stream(snapshot)
