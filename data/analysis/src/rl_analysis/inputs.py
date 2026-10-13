"""Read one dbt run's marts and feature mart into an in-memory DuckDB database.

Layout (``data/dbt/macros/external_location.sql``; ``<root>`` is ``s3://<bucket>`` or a local
directory such as the ``fixture`` target's output root)::

    <root>/marts/<model>/run_id=<run_id>/part-00000.parquet
    <root>/features/release_features/schema=<v>/run_id=<run_id>/part-00000.parquet
    <root>/features/release_features/schema=<v>/run_id=<run_id>/_manifest.json

Every query parameter is bound (``?``); the only interpolated SQL is the S3 secret and the
DuckDB settings, quoted by :func:`sql_literal`. Tables created in the session:

* ``builds``: the ``fct_builds`` columns the analysis uses (all builds, labelled or not);
* ``features``: ``build_id, label, prev_build_failed`` of ``mart_build_features``;
* ``change_risk`` / ``reliability``: ``mart_change_risk`` / ``mart_build_reliability``, for the
  cross-checks;
* ``naive``: the NAIVE previous-build link, a mirror of ``int_build_history`` (dbt keeps that
  model in its DuckDB file only, so it is not part of a run's published outputs).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb

from rl_analysis.settings import AnalysisSettings

PART_FILENAME: Final = "part-00000.parquet"
MART_MODELS: Final = ("fct_builds", "mart_change_risk", "mart_build_reliability")
_SAFE_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InputError(ValueError):
    """A run's inputs are missing, malformed or inconsistent with each other."""


def sql_literal(value: str) -> str:
    """A single-quoted SQL string literal (embedded quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


def _require_segment(value: str, name: str) -> str:
    if not _SAFE_SEGMENT_RE.fullmatch(value):
        raise InputError(f"{name} {value!r} is not a safe path segment")
    return value


@dataclass(frozen=True, slots=True)
class RunInputs:
    """The published outputs of one dbt run (``run_id``) under ``root``."""

    root: str
    run_id: str
    feature_schema_version: str

    @staticmethod
    def create(root: str, run_id: str, feature_schema_version: str) -> RunInputs:
        _require_segment(run_id, "run_id")
        _require_segment(feature_schema_version, "feature_schema_version")
        if root.startswith("s3://"):
            if len(root.rstrip("/")) <= len("s3://"):
                raise InputError(f"no bucket in input root {root!r}")
            norm = root.rstrip("/")
        else:
            norm = Path(root).expanduser().resolve().as_posix()
        return RunInputs(norm, run_id, feature_schema_version)

    @property
    def is_s3(self) -> bool:
        return self.root.startswith("s3://")

    def mart(self, model: str) -> str:
        return f"{self.root}/marts/{model}/run_id={self.run_id}/{PART_FILENAME}"

    @property
    def features_dir(self) -> str:
        return (
            f"{self.root}/features/release_features/schema={self.feature_schema_version}/"
            f"run_id={self.run_id}"
        )

    @property
    def features(self) -> str:
        return f"{self.features_dir}/{PART_FILENAME}"

    @property
    def feature_manifest(self) -> str:
        return f"{self.features_dir}/_manifest.json"


def connect(settings: AnalysisSettings, *, s3: bool) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB session (UTC); with ``s3`` also httpfs and a temporary S3 secret."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"SET memory_limit = {sql_literal(settings.duckdb_memory_limit)}")
    con.execute(f"SET threads = {int(settings.duckdb_threads)}")
    if s3:
        endpoint = re.sub(r"^https?://", "", settings.s3_endpoint).rstrip("/")
        use_ssl = "true" if settings.s3_endpoint.startswith("https://") else "false"
        con.execute("LOAD httpfs")
        con.execute(
            "CREATE OR REPLACE TEMPORARY SECRET rl_analysis_s3 (TYPE s3, "
            f"KEY_ID {sql_literal(settings.s3_access_key)}, "
            f"SECRET {sql_literal(settings.s3_secret_key.get_secret_value())}, "
            f"REGION {sql_literal(settings.s3_region)}, ENDPOINT {sql_literal(endpoint)}, "
            f"URL_STYLE 'path', USE_SSL {use_ssl})"
        )
    return con


_LOAD_BUILDS: Final = """
CREATE TEMP TABLE builds AS
SELECT build_id, project, started_at, started_month, finished_at_upper, status, label,
       duration_s, is_pr, by_core_team_member, src_churn, test_churn, total_churn, churn_bucket
FROM read_parquet(?, hive_partitioning = false)
"""
_LOAD_FEATURES: Final = """
CREATE TEMP TABLE features AS
SELECT build_id, label, prev_build_failed
FROM read_parquet(?, hive_partitioning = false)
"""
_LOAD_CHANGE_RISK: Final = """
CREATE TEMP TABLE change_risk AS
SELECT dimension, bucket, bucket_order, n_labelled_builds, n_failures
FROM read_parquet(?, hive_partitioning = false)
"""
_LOAD_RELIABILITY: Final = """
CREATE TEMP TABLE reliability AS
SELECT project, month_start, n_labelled_builds, n_failures
FROM read_parquet(?, hive_partitioning = false)
"""
# Mirror of data/dbt/models/intermediate/int_build_history.sql (NAIVE link, analytics only): the
# project's build that STARTED just before (order started_at, build_id), whether or not it had
# finished. Over ALL builds, as in dbt (a canceled predecessor has no outcome: label NULL).
_NAIVE_LINK: Final = """
CREATE TEMP TABLE naive AS
SELECT
    build_id,
    label,
    lag(build_id) OVER w AS naive_prev_build_id,
    lag(label) OVER w AS naive_prev_label,
    CASE
        WHEN lag(build_id) OVER w IS NULL THEN NULL
        WHEN lag(finished_at_upper) OVER w IS NULL THEN NULL
        ELSE lag(finished_at_upper) OVER w < started_at
    END AS naive_prev_finished_before_start
FROM builds
WINDOW w AS (PARTITION BY project ORDER BY started_at, build_id)
"""


def _file_facts(con: duckdb.DuckDBPyConnection, location: str) -> dict[str, Any]:
    row = con.execute("SELECT content FROM read_blob(?)", [location]).fetchone()
    if row is None:
        raise InputError(f"cannot read {location}")
    content = bytes(row[0])
    return {
        "location": location,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def load(con: duckdb.DuckDBPyConnection, inputs: RunInputs) -> dict[str, Any]:
    """Create the session tables; return provenance (locations, bytes, sha256, rows, manifest).

    Raises :class:`InputError` when a file is missing or the feature manifest disagrees with
    the run (run id, schema version, the features file's sha256).
    """
    try:
        manifest_row = con.execute(
            "SELECT content FROM read_text(?)", [inputs.feature_manifest]
        ).fetchone()
        con.execute(_LOAD_BUILDS, [inputs.mart("fct_builds")])
        con.execute(_LOAD_FEATURES, [inputs.features])
        con.execute(_LOAD_CHANGE_RISK, [inputs.mart("mart_change_risk")])
        con.execute(_LOAD_RELIABILITY, [inputs.mart("mart_build_reliability")])
        files = {model: _file_facts(con, inputs.mart(model)) for model in MART_MODELS}
        files["mart_build_features"] = _file_facts(con, inputs.features)
    except duckdb.Error as exc:
        raise InputError(f"cannot load run {inputs.run_id!r} from {inputs.root}: {exc}") from exc
    if manifest_row is None:
        raise InputError(f"empty feature manifest {inputs.feature_manifest}")
    manifest = json.loads(str(manifest_row[0]))
    con.execute(_NAIVE_LINK)

    files["fct_builds"]["rows"] = _count(con, "SELECT count(*) FROM builds")
    files["mart_change_risk"]["rows"] = _count(con, "SELECT count(*) FROM change_risk")
    files["mart_build_reliability"]["rows"] = _count(con, "SELECT count(*) FROM reliability")
    files["mart_build_features"]["rows"] = _count(con, "SELECT count(*) FROM features")

    problems = []
    if manifest.get("run_id") != inputs.run_id:
        problems.append(f"manifest run_id {manifest.get('run_id')!r} != {inputs.run_id!r}")
    if manifest.get("feature_schema_version") != inputs.feature_schema_version:
        problems.append(
            f"manifest feature_schema_version {manifest.get('feature_schema_version')!r} "
            f"!= {inputs.feature_schema_version!r}"
        )
    m_file = manifest.get("file") or {}
    if m_file.get("sha256") != files["mart_build_features"]["sha256"]:
        problems.append("features file sha256 differs from its manifest")
    if m_file.get("rows") != files["mart_build_features"]["rows"]:
        problems.append("features row count differs from its manifest")
    if problems:
        raise InputError("; ".join(problems))
    return {
        "root": inputs.root,
        "run_id": inputs.run_id,
        "feature_schema_version": inputs.feature_schema_version,
        "files": dict(sorted(files.items())),
        "feature_manifest": {
            "location": inputs.feature_manifest,
            "dataset": manifest.get("dataset"),
            "dataset_version": manifest.get("dataset_version"),
            "label_rate": manifest.get("label_rate"),
            "cutpoints": manifest.get("cutpoints"),
        },
    }


_NAIVE_CHECK: Final = """
SELECT
    count(*) AS n,
    count(*) FILTER (WHERE m.build_id IS NULL OR h.build_id IS NULL) AS missing,
    count(*) FILTER (
        WHERE m.naive_prev_build_id IS DISTINCT FROM h.naive_prev_build_id
           OR m.naive_prev_label IS DISTINCT FROM h.naive_prev_label
           OR m.naive_prev_finished_before_start IS DISTINCT FROM h.naive_prev_finished_before_start
    ) AS mismatched
FROM naive AS m
FULL OUTER JOIN rl_dbt.main.int_build_history AS h ON m.build_id = h.build_id
"""


def check_naive_link(con: duckdb.DuckDBPyConnection, duckdb_path: Path) -> dict[str, Any]:
    """Compare the ``naive`` mirror with dbt's ``int_build_history`` in its DuckDB file.

    The file is attached read-only. It holds the intermediates of the LAST dbt build, which must
    be the analysed run (the report records which file was compared; a mismatch is an error).
    """
    path = duckdb_path.expanduser().resolve()
    if not path.is_file():
        raise InputError(f"no DuckDB file at {path}")
    try:
        con.execute(f"ATTACH {sql_literal(path.as_posix())} AS rl_dbt (READ_ONLY)")
        row = con.execute(_NAIVE_CHECK).fetchone()
    except duckdb.Error as exc:
        raise InputError(f"cannot compare with int_build_history in {path}: {exc}") from exc
    finally:
        con.execute("DETACH DATABASE IF EXISTS rl_dbt")
    n, missing, mismatched = (int(v) for v in row) if row else (0, 0, 0)
    return {
        "status": "ok" if missing == 0 and mismatched == 0 and n > 0 else "failed",
        "compared_with": "int_build_history",
        "duckdb_file": path.name,
        "rows": n,
        "missing_rows": missing,
        "mismatched_rows": mismatched,
    }
