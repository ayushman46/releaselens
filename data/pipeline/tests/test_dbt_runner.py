"""dbt_runner: context resolution, run ids, output layout, skip semantics (no dbt invocation)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rl_core.features.schema import FEATURE_SCHEMA_VERSION
from rl_pipeline import dbt_runner
from rl_pipeline.datasets import TT_5_3_2016, TT_FINAL_2017
from rl_pipeline.dbt_runner import (
    SKIP_EXIT_CODE,
    DbtNotApplicableError,
    make_context,
    sanitize_run_id,
    skip_if_not_applicable,
)
from rl_pipeline.settings import PipelineSettings

MD5 = TT_5_3_2016.source.md5


def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "manual__2026-10-13T03:00:00.123+00:00",
            "manual__2026-10-13T03_00_00.123_00_00-" + _h("manual__2026-10-13T03:00:00.123+00:00"),
        ),
        (
            "scheduled__2026-10-13T00:00:00+00:00",
            "scheduled__2026-10-13T00_00_00_00_00-" + _h("scheduled__2026-10-13T00:00:00+00:00"),
        ),
        ("m3a-run1", "m3a-run1"),
        ("../../etc", "etc-" + _h("../../etc")),
        ("a/b c", "a_b_c-" + _h("a/b c")),
        ("", "manual"),
        (None, "manual"),
        ("x" * 128, "x" * 128),
        ("x" * 300, "x" * 117 + "-" + _h("x" * 300)),
        ("///", "run-" + _h("///")),
    ],
)
def test_sanitize_run_id(raw: str | None, expected: str) -> None:
    out = sanitize_run_id(raw)
    assert out == expected
    assert len(out) <= 128
    assert sanitize_run_id(out) == out  # idempotent: a task result carries its id forward


def test_distinct_run_ids_never_share_an_output_prefix() -> None:
    """Sanitising alone would map these to one run_id= segment (the later run overwrites)."""
    ids = ["a:b", "a/b", "a b", "a_b", "x" * 200, "x" * 200 + "y", "-a", "a"]
    assert len({sanitize_run_id(i) for i in ids}) == len(ids)


def test_defaults_point_at_the_primary_on_s3(pipeline_env: dict[str, Path]) -> None:
    ctx = make_context({"run_id": "manual__2026-10-13T03:00:00+00:00"})
    assert ctx.dataset == TT_5_3_2016.key
    assert ctx.dataset_version == MD5
    assert ctx.target == "dev"
    assert ctx.raw_root == "s3://releaselens-data/raw/travistorrent"
    assert ctx.output_root == "s3://releaselens-data"
    assert ctx.run_id == "manual__2026-10-13T03_00_00_00_00-" + _h(
        "manual__2026-10-13T03:00:00+00:00"
    )
    assert ctx.work_dir == (pipeline_env["lake"] / "dbt").resolve()
    assert ctx.duckdb_path.name == "releaselens.duckdb"
    assert (ctx.project_dir / "dbt_project.yml").is_file()
    assert ctx.vars == {
        "dataset_version": MD5,
        "run_id": ctx.run_id,
        "raw_root": ctx.raw_root,
        "output_root": ctx.output_root,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }


def test_output_layout(pipeline_env: dict[str, Path]) -> None:
    ctx = make_context({"run_id": "r1"})
    assert ctx.output_location("fct_builds") == (
        "s3://releaselens-data/marts/fct_builds/run_id=r1/part-00000.parquet"
    )
    assert ctx.output_location("mart_build_features") == (
        f"s3://releaselens-data/features/release_features/schema={FEATURE_SCHEMA_VERSION}/"
        "run_id=r1/part-00000.parquet"
    )


def test_previous_task_result_carries_the_context(pipeline_env: dict[str, Path]) -> None:
    first = make_context({"run_id": "r1", "vars": {"label_rate_min": 0.1}})
    again = make_context({**first.summary(), "dbt": {"nodes": []}, "outputs": []})
    assert again == first


def test_secondary_dataset_is_not_applicable(pipeline_env: dict[str, Path]) -> None:
    with pytest.raises(DbtNotApplicableError, match="primary dataset"):
        make_context({"dataset": TT_FINAL_2017.key})
    with pytest.raises(DbtNotApplicableError):
        make_context({"version": TT_FINAL_2017.source.md5})  # selected by its md5


def test_skip_helper_turns_not_applicable_into_the_skip_exit_code(
    pipeline_env: dict[str, Path],
) -> None:
    with pytest.raises(SystemExit) as exc, skip_if_not_applicable():
        make_context({"dataset": TT_FINAL_2017.key})
    assert exc.value.code == SKIP_EXIT_CODE
    with pytest.raises(ValueError, match="unknown dbt target"), skip_if_not_applicable():
        make_context({"target": "prod"})  # other errors propagate unchanged


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"target": "prod"}, "unknown dbt target"),
        ({"version": "0" * 32}, "not the primary dataset's md5"),
        ({"target": "fixture"}, "local directories only"),
        ({"target": "fixture", "version": "x", "raw_root": "/a", "output_root": "/b"}, "md5"),
        ({"output_root": "s3://bucket/it's"}, "unsupported character"),
        ({"vars": {"run_id": "x"}}, "cannot be overridden"),
        ({"project_dir": "/nonexistent"}, "no dbt_project.yml"),
    ],
)
def test_invalid_params_are_rejected(
    pipeline_env: dict[str, Path], params: dict[str, object], match: str
) -> None:
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        make_context(params)


def test_project_dir_setting(
    pipeline_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RL_DBT_PROJECT_DIR", "/opt/airflow/dbt")
    assert PipelineSettings().dbt_project_dir == Path("/opt/airflow/dbt")


def test_env_carries_settings_and_disables_tracking(pipeline_env: dict[str, Path]) -> None:
    ctx = make_context({"run_id": "r1"})
    env = dbt_runner._env(ctx, 123)
    assert env["RL_LAKE_DIR"] == str(ctx.work_dir.parent)
    assert env["RL_DBT_MAX_TEMP_SIZE"] == "123B"
    assert env["RL_DBT_TARGET"] == "dev"
    assert env["RL_S3_ENDPOINT"].startswith("http")
    assert env["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false"
    assert env["RL_DBT_PACKAGES_DIR"] == str(ctx.work_dir / "dbt_packages")


def test_deps_takes_neither_target_nor_target_path(pipeline_env: dict[str, Path]) -> None:
    ctx = make_context({"run_id": "r1"})
    deps = dbt_runner._common_args(ctx, "deps")
    build = dbt_runner._common_args(ctx, "build")
    assert "--target" not in deps
    assert "--target-path" not in deps
    assert {"--target", "--target-path", "--vars", "--log-path"} <= set(build)


def test_a_stage_that_builds_no_model_fails(
    pipeline_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selector matching nothing must not 'succeed' and report an older file of the run id."""
    ctx = make_context({"run_id": "r1"})
    empty = {"command": "build", "success": True, "nodes": [], "status_counts": {}}
    monkeypatch.setattr(dbt_runner, "invoke", lambda *_a, **_k: empty)
    monkeypatch.setattr(
        dbt_runner, "measure_outputs", lambda *_a, **_k: pytest.fail("must not measure")
    )
    with pytest.raises(dbt_runner.DbtStageError, match="built no model"):
        dbt_runner.build_stage(ctx, "marts")
