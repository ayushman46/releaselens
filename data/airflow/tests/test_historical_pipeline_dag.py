"""DAG integrity tests for historical_pipeline.

They run inside the rlens/airflow image (`make pipeline-test-dag`), because Airflow lives in its
own venv (ADR-0013) and is not part of the uv workspace. No database or S3 needed. In the
workspace venv (`uv run pytest`, CI unit jobs) the module is reported as skipped instead of
breaking collection.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("airflow", reason="Airflow lives in its own venv: run `make pipeline-test-dag`")

from airflow.dag_processing.dagbag import DagBag
from airflow.sdk.exceptions import AirflowSkipException

DAGS_DIR = Path(os.environ.get("AIRFLOW__CORE__DAGS_FOLDER", "/opt/airflow/dags"))
WORKSPACE_PYTHON = "/app/.venv/bin/python"
PRIMARY = "travistorrent-5-3-2016"
SECONDARY = "travistorrent-final-2017-01-25"
SUBDIRS = {
    PRIMARY: "travistorrent-figshare-19314170-5_3_2016",
    SECONDARY: "travistorrent-figshare-19314170",
}

EXPECTED_ORDER = [
    "download_or_locate_dataset",
    "validate_raw_schema",
    "convert_to_parquet",
    "load_raw_s3",
    "dbt_deps",
    "dbt_build_staging",
    "dbt_build_intermediate",
    "dbt_build_marts",
    "export_analytics_to_postgres",
    "build_feature_snapshot",
    "training_enabled",
    "train_models",
    "evaluate_and_register",
]
DBT_TASKS = [
    "dbt_deps",
    "dbt_build_staging",
    "dbt_build_intermediate",
    "dbt_build_marts",
    "build_feature_snapshot",
]
IMPLEMENTED = EXPECTED_ORDER[:4] + DBT_TASKS
PLACEHOLDERS = {
    "export_analytics_to_postgres": "M3b",
    "train_models": "M4",
    "evaluate_and_register": "M4",
}
CONTRACT = {
    "download_or_locate_dataset": "locate_or_download",
    "validate_raw_schema": "validate_raw_schema",
    "convert_to_parquet": "convert_to_parquet",
    "load_raw_s3": "load_raw_s3",
    **{task_id: task_id for task_id in DBT_TASKS},
}
SKIP_EXIT_CODE = 99


@pytest.fixture(scope="module")
def dag() -> Any:
    bag = DagBag(dag_folder=str(DAGS_DIR))
    assert bag.import_errors == {}, bag.import_errors
    assert "historical_pipeline" in bag.dags
    return bag.dags["historical_pipeline"]


def test_dag_settings(dag: Any) -> None:
    assert dag.schedule is None
    assert dag.catchup is False
    assert dag.max_active_runs == 1
    assert dag.params["dataset"] == PRIMARY
    assert dag.params.get_param("dataset").schema["enum"] == [PRIMARY, SECONDARY]
    assert dag.params["skip_training"] is True
    assert dag.params["dataset_dir"] is None
    assert dag.params["allow_download"] is True


def test_dataset_param_rejects_unknown_keys(dag: Any) -> None:
    # resolve() stores the value on the Param: work on a copy, the dag fixture is module-scoped
    param = copy.deepcopy(dag.params.get_param("dataset"))
    assert param.resolve(SECONDARY) == SECONDARY
    with pytest.raises(Exception) as err:  # class location varies across Airflow 3.x
        param.resolve("travistorrent-8-2-2017")
    assert type(err.value).__name__ == "ParamValidationError"


def test_tasks_are_a_single_chain_in_spec_order(dag: Any) -> None:
    assert sorted(dag.task_ids) == sorted(EXPECTED_ORDER)
    for upstream, downstream in itertools.pairwise(EXPECTED_ORDER):
        assert downstream in dag.get_task(upstream).downstream_task_ids, (upstream, downstream)
    # the only extra edges: convert also consumes the located dataset dict, and the feature
    # snapshot the marts' dbt context (both XComs)
    assert dag.get_task("convert_to_parquet").upstream_task_ids == {
        "download_or_locate_dataset",
        "validate_raw_schema",
    }
    assert dag.get_task("build_feature_snapshot").upstream_task_ids == {
        "export_analytics_to_postgres",
        "dbt_build_marts",
    }


@pytest.mark.parametrize("task_id", IMPLEMENTED)
def test_implemented_tasks_run_in_workspace_venv(dag: Any, task_id: str) -> None:
    op = dag.get_task(task_id)
    assert op.python == WORKSPACE_PYTHON
    assert op.expect_airflow is False
    assert op.retries == 2
    assert op.retry_delay == timedelta(seconds=30)
    assert op.retry_exponential_backoff == 2.0
    assert op.execution_timeout is not None
    assert op.execution_timeout >= timedelta(minutes=10)
    source = op.get_python_source()
    assert "@task" not in source  # decorator stripped: the body is shipped as plain source
    assert f"from rl_pipeline.tasks import {CONTRACT[task_id]}" in source
    compile(source, f"<{task_id}>", "exec")


@pytest.mark.parametrize("task_id", DBT_TASKS)
def test_dbt_tasks_skip_on_exit_code_and_trigger_rules(dag: Any, task_id: str) -> None:
    op = dag.get_task(task_id)
    assert list(op.skip_on_exit_code) == [SKIP_EXIT_CODE]
    expected_rule = "none_failed" if task_id == "build_feature_snapshot" else "all_success"
    assert op.trigger_rule == expected_rule
    assert op.execution_timeout >= timedelta(minutes=10)
    if task_id == "dbt_deps":
        assert op.op_kwargs["airflow_run_id"] == "{{ run_id }}"
    assert "skip_if_not_applicable" in op.get_python_source()


@pytest.mark.parametrize(("task_id", "milestone"), sorted(PLACEHOLDERS.items()))
def test_placeholders_skip_with_reason(dag: Any, task_id: str, milestone: str) -> None:
    op = dag.get_task(task_id)
    assert op.trigger_rule == "none_failed"
    with pytest.raises(AirflowSkipException, match=f"implemented in {milestone}"):
        op.python_callable()


def test_training_gate_follows_skip_training_param(dag: Any) -> None:
    gate = dag.get_task("training_enabled").python_callable
    assert gate(params={"skip_training": True}) is False
    assert gate(params={"skip_training": False}) is True
    assert gate(params={}) is False  # default: skip until M4


def test_dag_serialises(dag: Any) -> None:
    from airflow.serialization.serialized_objects import DagSerialization

    data = DagSerialization.to_dict(dag)
    assert data["dag"]["dag_id"] == "historical_pipeline"


@pytest.mark.skipif(not Path(WORKSPACE_PYTHON).exists(), reason="workspace venv not in image")
def test_dataset_keys_match_registry(dag: Any) -> None:
    """The DAG's enum (parsed in the Airflow venv) mirrors rl_pipeline.datasets exactly."""
    code = (
        "import json; from rl_pipeline.datasets import DATASETS, DEFAULT_DATASET; "
        "print(json.dumps({'keys': list(DATASETS), 'default': DEFAULT_DATASET, "
        "'subdirs': {k: s.subdir for k, s in DATASETS.items()}}))"
    )
    out = subprocess.run(
        [WORKSPACE_PYTHON, "-c", code], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    registry = json.loads(out.stdout.strip().splitlines()[-1])
    assert dag.params.get_param("dataset").schema["enum"] == registry["keys"]
    assert dag.params["dataset"] == registry["default"]
    assert registry["subdirs"] == SUBDIRS


@pytest.mark.skipif(not Path(WORKSPACE_PYTHON).exists(), reason="workspace venv not in image")
def test_contract_functions_exist_in_workspace_venv() -> None:
    names = ", ".join(sorted(set(CONTRACT.values())))
    code = (
        f"from rl_pipeline.tasks import {names}; "
        "from rl_pipeline.__main__ import configure_cli_logging; print('ok')"
    )
    out = subprocess.run(
        [WORKSPACE_PYTHON, "-c", code], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.skipif(not Path(WORKSPACE_PYTHON).exists(), reason="workspace venv not in image")
def test_skip_exit_code_matches_workspace_venv() -> None:
    code = "from rl_pipeline.dbt_runner import SKIP_EXIT_CODE; print(SKIP_EXIT_CODE)"
    out = subprocess.run(
        [WORKSPACE_PYTHON, "-c", code], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == SKIP_EXIT_CODE


@pytest.mark.skipif(not Path(WORKSPACE_PYTHON).exists(), reason="workspace venv not in image")
@pytest.mark.parametrize(
    ("task_id", "call"),
    [
        # the secondary dataset has no dbt models: real rl_pipeline code -> exit code 99
        (
            "dbt_deps",
            "dbt_deps({'dataset': 'travistorrent-final-2017-01-25', "
            "'version': '5d359345fecf0bf47be6edb10b5aae58'}, 'manual__x')",
        ),
        ("dbt_build_marts", "dbt_build_marts({'dataset': 'travistorrent-final-2017-01-25'})"),
        # no upstream dbt context (dbt_build_marts was skipped) -> skipped as well
        ("build_feature_snapshot", "build_feature_snapshot(None)"),
    ],
)
def test_dbt_tasks_exit_with_skip_code_without_dbt(
    dag: Any, tmp_path: Path, task_id: str, call: str
) -> None:
    source = textwrap.dedent(dag.get_task(task_id).get_python_source())
    script = tmp_path / "run_task.py"
    script.write_text("from typing import Any\n" + source + "\n" + call + "\n", encoding="utf-8")
    env = {**os.environ, "RL_LAKE_DIR": str(tmp_path / "lake")}
    out = subprocess.run(
        [WORKSPACE_PYTHON, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert out.returncode == SKIP_EXIT_CODE, out.stderr


# The real task body, run in the workspace venv against a fake
# rl_pipeline.dataset.locate_or_download (no network): where does it look, where would it download?
_FAKE_LOCATE = """
from __future__ import annotations

import json
import sys

import rl_pipeline.dataset as ds

CALLS = []


def _fake_locate(dataset_dir, source=ds.TRAVISTORRENT_5_3_2016, *, allow_download=True, **_):
    CALLS.append(
        {"dir": str(dataset_dir), "allow_download": allow_download, "file": source.filename}
    )
    if not allow_download:
        raise ds.DatasetMissingError(f"{dataset_dir} holds no copy (fake)")
    path = str(dataset_dir / source.filename)
    return ds.LocatedDataset(path, source.md5, source.size, source.md5, downloaded=True)


ds.locate_or_download = _fake_locate
"""

_CALL = """
try:
    result = download_or_locate_dataset(sys.argv[1], sys.argv[2], sys.argv[3])
except ds.DatasetError as exc:
    result = {"error": type(exc).__name__}
print(json.dumps({"calls": CALLS, "result": result}))
"""


FILES = {PRIMARY: "travistorrent_5_3_2016.csv.gz", SECONDARY: "final-2017-01-25.csv.gz"}


@pytest.mark.skipif(not Path(WORKSPACE_PYTHON).exists(), reason="workspace venv not in image")
@pytest.mark.parametrize("dataset_key", ["", PRIMARY, SECONDARY])
@pytest.mark.parametrize(
    ("writable", "allow_download", "expected_calls", "expected_result"),
    [
        (True, "True", [("data", True)], "data"),
        (False, "True", [("data", False), ("lake", True)], "lake"),
        (False, "False", [("data", False)], "DatasetMissingError"),
    ],
)
def test_download_never_targets_a_read_only_dataset_dir(
    dag: Any,
    tmp_path: Path,
    dataset_key: str,
    writable: bool,
    allow_download: str,
    expected_calls: list[tuple[str, bool]],
    expected_result: str,
) -> None:
    key = dataset_key or PRIMARY  # an empty param means the registry default
    datasets = tmp_path / "datasets"  # RL_DATASETS_DIR (compose: /datasets, read-only)
    data_dir = datasets / SUBDIRS[key]
    data_dir.mkdir(parents=True)
    if not writable:
        data_dir.chmod(0o555)  # like the compose `:ro` mount
    lake = tmp_path / "lake"
    dirs = {"data": str(data_dir), "lake": str(lake / "downloads" / SUBDIRS[key])}
    source = textwrap.dedent(dag.get_task("download_or_locate_dataset").get_python_source())
    script = tmp_path / "run_task.py"
    script.write_text(_FAKE_LOCATE + "\n" + source + "\n" + _CALL, encoding="utf-8")
    env = {**os.environ, "RL_DATASETS_DIR": str(datasets), "RL_LAKE_DIR": str(lake)}
    env.pop("RL_DATASET_DIR", None)

    out = subprocess.run(
        [WORKSPACE_PYTHON, str(script), dataset_key, "", allow_download],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["calls"] == [
        {"dir": dirs[d], "allow_download": a, "file": FILES[key]} for d, a in expected_calls
    ]
    if expected_result in dirs:
        assert report["result"]["path"].startswith(dirs[expected_result] + "/")
        assert report["result"]["dataset"] == key
    else:
        assert report["result"] == {"error": expected_result}
    assert list(data_dir.iterdir()) == []  # the read-only dir is never written to
