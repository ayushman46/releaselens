"""DAG integrity tests for historical_pipeline.

They run inside the rlens/airflow image (`make pipeline-test-dag`), because Airflow lives in its
own venv (ADR-0013) and is not part of the uv workspace. No database or S3 needed. In the
workspace venv (`uv run pytest`, CI unit jobs) the module is reported as skipped instead of
breaking collection.
"""

from __future__ import annotations

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
IMPLEMENTED = EXPECTED_ORDER[:4]
PLACEHOLDERS = {
    "dbt_deps": "M3",
    "dbt_build_staging": "M3",
    "dbt_build_intermediate": "M3",
    "dbt_build_marts": "M3",
    "export_analytics_to_postgres": "M3",
    "build_feature_snapshot": "M3",
    "train_models": "M4",
    "evaluate_and_register": "M4",
}
CONTRACT = {
    "download_or_locate_dataset": "locate_or_download",
    "validate_raw_schema": "validate_raw_schema",
    "convert_to_parquet": "convert_to_parquet",
    "load_raw_s3": "load_raw_s3",
}


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
    assert dag.params["skip_training"] is True
    assert dag.params["dataset_dir"] is None
    assert dag.params["allow_download"] is True


def test_tasks_are_a_single_chain_in_spec_order(dag: Any) -> None:
    assert sorted(dag.task_ids) == sorted(EXPECTED_ORDER)
    for upstream, downstream in itertools.pairwise(EXPECTED_ORDER):
        assert downstream in dag.get_task(upstream).downstream_task_ids, (upstream, downstream)
    # the only extra edge: convert also consumes the located dataset dict (XCom)
    assert dag.get_task("convert_to_parquet").upstream_task_ids == {
        "download_or_locate_dataset",
        "validate_raw_schema",
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


# The real task body, run in the workspace venv against a fake
# rl_pipeline.dataset.locate_or_download (no network): where does it look, where would it download?
_FAKE_LOCATE = """
from __future__ import annotations

import json
import sys

import rl_pipeline.dataset as ds

CALLS = []


def _fake_locate(dataset_dir, source=ds.TRAVISTORRENT_FIGSHARE, *, allow_download=True, **_):
    CALLS.append({"dir": str(dataset_dir), "allow_download": allow_download})
    if not allow_download:
        raise ds.DatasetMissingError(f"{dataset_dir} holds no copy (fake)")
    path = str(dataset_dir / source.filename)
    return ds.LocatedDataset(path, source.md5, source.size, source.md5, downloaded=True)


ds.locate_or_download = _fake_locate
"""

_CALL = """
try:
    result = download_or_locate_dataset(sys.argv[1], sys.argv[2])
except ds.DatasetError as exc:
    result = {"error": type(exc).__name__}
print(json.dumps({"calls": CALLS, "result": result}))
"""


@pytest.mark.skipif(not Path(WORKSPACE_PYTHON).exists(), reason="workspace venv not in image")
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
    writable: bool,
    allow_download: str,
    expected_calls: list[tuple[str, bool]],
    expected_result: str,
) -> None:
    data_dir = tmp_path / "datasets"
    data_dir.mkdir()
    if not writable:
        data_dir.chmod(0o555)  # like the compose `:ro` mount
    lake = tmp_path / "lake"
    dirs = {"data": str(data_dir), "lake": str(lake / "downloads/travistorrent-figshare-19314170")}
    source = textwrap.dedent(dag.get_task("download_or_locate_dataset").get_python_source())
    script = tmp_path / "run_task.py"
    script.write_text(_FAKE_LOCATE + "\n" + source + "\n" + _CALL, encoding="utf-8")
    env = {**os.environ, "RL_DATASET_DIR": str(data_dir), "RL_LAKE_DIR": str(lake)}

    out = subprocess.run(
        [WORKSPACE_PYTHON, str(script), "", allow_download],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["calls"] == [{"dir": dirs[d], "allow_download": a} for d, a in expected_calls]
    if expected_result in dirs:
        assert report["result"]["path"].startswith(dirs[expected_result] + "/")
    else:
        assert report["result"] == {"error": expected_result}
    assert list(data_dir.iterdir()) == []  # the read-only dir is never written to
