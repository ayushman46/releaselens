"""End-to-end ``dbt build`` on the SYNTHETIC 2016 fixture (target ``fixture``: local Parquet).

The same project, models, tests and runner as the real build; only the roots differ. Checks the
whole chain (staging -> intermediate -> marts -> ml incl. the rl_core Python model), that the
Python mirror of the output layout matches the dbt macro, determinism across run ids, and that a
failing dbt test fails the task.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest

from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from rl_pipeline import tasks
from rl_pipeline.dbt_runner import DbtStageError, make_context

EXTERNAL_MODELS = {
    "dim_project",
    "dim_time",
    "fct_builds",
    "mart_build_reliability",
    "mart_change_risk",
    "mart_ci_performance",
    "mart_build_inputs",
    "split_cutpoints",
    "mart_build_features",
}


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("dbt-fixture")
    mp = pytest.MonkeyPatch()
    mp.setenv("RL_LAKE_DIR", str(work / "lake"))
    mp.setenv("RL_DUCKDB_MEMORY_LIMIT", "512MB")
    mp.setenv("RL_DUCKDB_THREADS", "2")
    mp.setenv("RL_DUCKDB_MAX_TEMP_MIB", "128")
    mp.setenv("RL_ENV", "test")
    try:
        result = tasks.dbt_build_fixture({"work_dir": str(work / "fx"), "run_id": "fx-1"})
    finally:
        mp.undo()
    return result


def _outputs(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["model"]: o for step in result["steps"].values() for o in step.get("outputs", [])}


def test_every_stage_passes(fixture_run: dict[str, Any]) -> None:
    steps = fixture_run["steps"]
    assert list(steps) == [
        "dbt_deps",
        "dbt_build_staging",
        "dbt_build_intermediate",
        "dbt_build_marts",
        "build_feature_snapshot",
    ]
    for name, step in steps.items():
        assert step["dbt"]["success"] is True, name
        statuses = {n["status"] for n in step["dbt"]["nodes"]}
        assert statuses <= {"success", "pass"}, (name, statuses)
    tests_run = sum(
        1 for step in steps.values() for n in step["dbt"]["nodes"] if n["resource_type"] == "test"
    )
    assert tests_run >= 80  # every data test of the project ran in some stage
    assert fixture_run == json.loads(json.dumps(fixture_run))  # XCom-safe


def test_row_counts_on_the_fixture(fixture_run: dict[str, Any]) -> None:
    rows = {m: o["rows"] for m, o in _outputs(fixture_run).items()}
    # 18 java job rows (one foreign row) -> 16 jobs, 8 builds, 2 projects; 6 labelled builds
    assert rows["stg_travis_job_rows"] == 18
    assert rows["stg_travis_jobs"] == 16
    assert rows["stg_travis_builds"] == rows["fct_builds"] == rows["mart_build_inputs"] == 8
    assert rows["dim_project"] == 2
    assert rows["mart_build_features"] == 6
    assert rows["split_cutpoints"] == 1


def test_outputs_land_where_the_runner_expects(fixture_run: dict[str, Any]) -> None:
    outputs = _outputs(fixture_run)
    assert {m for m, o in outputs.items() if "location" in o} == EXTERNAL_MODELS
    work = Path(fixture_run["work_dir"])
    for model in EXTERNAL_MODELS:
        location = Path(outputs[model]["location"])
        assert location.is_file(), model
        assert location.is_relative_to(work / "out")
        assert location.name == "part-00000.parquet"
        expected = "release_features" if model == "mart_build_features" else model
        assert expected in location.parts
    features = Path(outputs["mart_build_features"]["location"])
    assert features.parent.name == "run_id=fx-1"
    assert features.parent.parent.name == f"schema={FEATURE_SCHEMA_VERSION}"


def test_feature_snapshot_report_and_manifest(fixture_run: dict[str, Any]) -> None:
    step = fixture_run["steps"]["build_feature_snapshot"]
    report = step["feature_snapshot"]
    assert report["cutpoints"]["n_labelled_builds"] == 6
    assert {k: v["rows"] for k, v in report["splits"].items()} == {
        "train": 4,
        "validation": 1,
        "test": 1,
    }
    assert report["label_rate"] == 0.5
    assert report["history_after_start_violations"] == 0
    # every fixture build finishes (start + duration) before the next split's first start
    assert report["cutpoints"]["n_train_not_finished_before_validation_start"] == 0
    assert report["cutpoints"]["n_validation_not_finished_before_test_start"] == 0
    assert report["columns"][:5] == ["build_id", "project", "started_at", "label", "split"]
    assert set(ALL_FEATURE_NAMES) <= set(report["columns"])
    manifest = json.loads(Path(step["manifest_location"]).read_text(encoding="utf-8"))
    assert manifest["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert manifest["run_id"] == "fx-1"
    assert manifest["file"]["sha256"] == _outputs(fixture_run)["mart_build_features"]["sha256"]


def test_label_and_build_grain_in_the_outputs(fixture_run: dict[str, Any]) -> None:
    outputs = _outputs(fixture_run)
    con = duckdb.connect()
    fct = outputs["fct_builds"]["location"]
    rows = con.execute(
        f"SELECT status, label, count(*) FROM read_parquet('{fct}', hive_partitioning = false) "
        "GROUP BY ALL ORDER BY ALL"
    ).fetchall()
    assert rows == [("canceled", None, 2), ("errored", 1, 3), ("passed", 0, 3)]
    inputs = outputs["mart_build_inputs"]["location"]
    unavailable = con.execute(
        "SELECT count(*) FROM read_parquet(?, hive_partitioning = false) WHERE "
        "num_commits_in_push IS NOT NULL OR description_complexity IS NOT NULL "
        "OR repo_age_days IS NOT NULL",
        [inputs],
    ).fetchone()
    assert unavailable == (0,)


def test_rebuild_with_another_run_id_is_byte_identical(
    fixture_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    work = Path(fixture_run["work_dir"])
    monkeypatch.setenv("RL_LAKE_DIR", str(work))
    monkeypatch.setenv("RL_DUCKDB_MAX_TEMP_MIB", "128")
    params = {
        "dataset": fixture_run["dataset"],
        "version": fixture_run["version"],
        "target": "fixture",
        "raw_root": fixture_run["raw_root"],
        "output_root": fixture_run["output_root"],
        "run_id": "fx-2",
    }
    marts = tasks.dbt_build_marts(params)
    ml = tasks.build_feature_snapshot(params)
    first = _outputs(fixture_run)
    for out in [*marts["outputs"], *ml["outputs"]]:
        assert "run_id=fx-2" in out["location"]
        assert out["sha256"] == first[out["model"]]["sha256"], out["model"]


def test_failing_dbt_test_fails_the_task(
    fixture_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    work = Path(fixture_run["work_dir"])
    monkeypatch.setenv("RL_LAKE_DIR", str(work))
    monkeypatch.setenv("RL_DUCKDB_MAX_TEMP_MIB", "128")
    params = {
        "dataset": fixture_run["dataset"],
        "version": fixture_run["version"],
        "target": "fixture",
        "raw_root": fixture_run["raw_root"],
        "output_root": fixture_run["output_root"],
        "run_id": "fx-bad",
        # the fixture's label rate is 0.5: an upper bound of 0.4 must fail label_rate_between
        "vars": {"label_rate_max": 0.4},
    }
    assert make_context(params).vars["label_rate_max"] == 0.4
    with pytest.raises(DbtStageError, match="label_rate_between") as err:
        tasks.dbt_build_marts(params)
    failed = [n for n in err.value.summary["nodes"] if n["status"] == "fail"]
    assert len(failed) == 1
    assert failed[0]["name"].startswith("label_rate_between_fct_builds")


def _compiled_test(work: Path, prefix: str) -> str:
    found = sorted((work / "dbt" / "target" / "compiled").rglob(f"{prefix}*.sql"))
    assert len(found) == 1, found
    return found[0].read_text(encoding="utf-8")


def test_leakage_oracle_runs_and_catches_a_window_the_audit_misses(
    fixture_run: dict[str, Any], tmp_path: Path
) -> None:
    """history_window_matches_inputs passes on the real output and fails on a leaked window.

    The tampered row counts one more past build than had finished (what admitting a build that
    finished at/after the prediction moment does): rl_core's own audit column is unchanged, so
    finished_before_started still passes, while the oracle (recomputed from mart_build_inputs)
    fails.
    """
    nodes = fixture_run["steps"]["build_feature_snapshot"]["dbt"]["nodes"]
    oracle_nodes = [n for n in nodes if n["name"].startswith("history_window_matches_inputs")]
    assert [n["status"] for n in oracle_nodes] == ["pass"]

    work = Path(fixture_run["work_dir"])
    oracle = _compiled_test(work, "history_window_matches_inputs_")
    audit = _compiled_test(work, "finished_before_started_")
    db = tmp_path / "releaselens_fixture.duckdb"  # same file stem = same catalog name
    shutil.copyfile(work / "dbt" / "releaselens_fixture.duckdb", db)
    con = duckdb.connect(str(db))
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE tampered AS SELECT * FROM main.mart_build_features")
    con.execute("DROP VIEW main.mart_build_features")
    con.execute("ALTER TABLE tampered RENAME TO mart_build_features")
    assert con.execute(oracle).fetchall() == []
    assert con.execute(audit).fetchall() == []
    con.execute(
        "UPDATE main.mart_build_features SET history_size = history_size + 1 "
        "WHERE build_id = (SELECT max(build_id) FROM main.mart_build_features)"
    )
    assert len(con.execute(oracle).fetchall()) == 1
    assert con.execute(audit).fetchall() == []  # the self-reported audit cannot see it
