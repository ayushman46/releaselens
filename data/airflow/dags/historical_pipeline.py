"""historical_pipeline: TravisTorrent (Figshare) to Parquet to S3, then dbt and ML (spec §7.2).

Dataset: ``params.dataset`` selects a registered release (``rl_pipeline.datasets``):
``travistorrent-5-3-2016`` (primary, default, ADR-0017) or ``travistorrent-final-2017-01-25``
(secondary). The key travels with every XCom dict, so all tasks use the same spec.

Implemented in M2:   download_or_locate_dataset -> validate_raw_schema -> convert_to_parquet
                     -> load_raw_s3
Implemented in M3a:  dbt_deps -> dbt_build_staging -> dbt_build_intermediate -> dbt_build_marts
                     -> [export_analytics_to_postgres: placeholder until M3b]
                     -> build_feature_snapshot (dbt ml/ models: rl_core features + split)
Placeholders:        training_enabled (short-circuit on params.skip_training)
                     -> train_models -> evaluate_and_register                    (M4)

Design (ADR-0013):
  * Airflow runs in its own venv. Every implemented task is a thin
    ``@task.external_python(python="/app/.venv/bin/python")`` wrapper around the Airflow-agnostic
    contract functions in ``rl_pipeline.tasks``. They take and return JSON-serialisable dicts
    (XCom-safe), so the DAG only wires XComs. Function bodies are shipped to the workspace
    interpreter as source, so they must import everything they use inside the body.
  * Idempotency lives in rl_pipeline: the dataset version is the md5 of the source file, and
    S3 objects are keyed by that version and skipped when the stored sha256 matches.
  * Data-quality failures raise inside rl_pipeline. The exception propagates, the task fails
    after its retries, and the DAG run fails. Nothing here catches it.
  * Placeholders raise AirflowSkipException ("implemented in M3/M4"). They are never reported
    as fake successes. They use trigger_rule NONE_FAILED so each one runs and logs its own
    reason instead of being skipped silently by propagation; a failure upstream still marks
    them upstream_failed. Real implementations go back to the default ALL_SUCCESS, except
    build_feature_snapshot, which follows the export placeholder and therefore uses
    NONE_FAILED (a failed dbt_build_marts still marks it upstream_failed).
  * dbt tasks (M3a) run the dbt-duckdb project through dbtRunner in the workspace venv
    (rl_pipeline.dbt_runner, ADR-0006). Each one passes its result dict (dataset, version, the
    sanitised Airflow run_id, roots) to the next. A failing model or test fails the task. dbt
    models exist for the primary dataset only: for another dataset the task exits with
    SKIP_EXIT_CODE, which Airflow records as skipped (skip_on_exit_code), not as a success.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from airflow.sdk import Param, TriggerRule, chain, dag, task
from airflow.sdk.exceptions import AirflowSkipException

log = logging.getLogger(__name__)

DAG_ID = "historical_pipeline"
WORKSPACE_PYTHON = "/app/.venv/bin/python"

# Mirrors rl_pipeline.datasets (the DAG is parsed in the Airflow venv, which cannot import
# rl_pipeline); test_dataset_keys_match_registry checks them against the workspace venv.
DATASETS = ("travistorrent-5-3-2016", "travistorrent-final-2017-01-25")
DEFAULT_DATASET = DATASETS[0]

DEFAULT_ARGS: dict[str, Any] = {
    "owner": "releaselens",
    "retries": 2,
    "retry_delay": timedelta(seconds=30),
    # Airflow >= 3.3 takes a float multiplier; 2.0 is what the legacy `True` meant (30 s, 60 s).
    "retry_exponential_backoff": 2.0,
    "max_retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=10),
}

# expect_airflow=False: the workspace venv has no Airflow (by design), so skip the probe/warning.
EXTERNAL: dict[str, Any] = {"python": WORKSPACE_PYTHON, "expect_airflow": False}

# rl_pipeline.dbt_runner.SKIP_EXIT_CODE: "dbt does not apply to this dataset" -> task skipped.
SKIP_EXIT_CODE = 99
DBT: dict[str, Any] = {**EXTERNAL, "skip_on_exit_code": SKIP_EXIT_CODE}

PLACEHOLDERS_M3 = ("export_analytics_to_postgres",)
PLACEHOLDERS_M4 = ("train_models", "evaluate_and_register")


# ---------------------------------------------------------------- M2: implemented tasks
@task.external_python(
    task_id="download_or_locate_dataset",
    execution_timeout=timedelta(minutes=30),
    **EXTERNAL,
)
def download_or_locate_dataset(
    dataset: str = "", dataset_dir: str = "", allow_download: str = "True"
) -> dict[str, Any]:
    """Find the dataset's Figshare file (or download it), verify its md5.

    Returns dataset/path/md5/size/version. Arguments arrive as rendered Jinja strings (op_kwargs
    templates), hence the str types; an empty ``dataset`` means the registry default.

    Compose mounts the host's downloads dir read-only at ``/datasets`` (each dataset in its own
    sub-directory), so a download (or the re-acquisition of a corrupt copy) can never happen
    there. When the dataset dir is not writable and holds no verified copy, the file is
    downloaded to ``<RL_LAKE_DIR>/downloads/<dataset subdir>`` instead (a writable volume); the
    read-only copy is never modified.
    """
    import logging
    import os
    from pathlib import Path

    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dataset import DatasetCorruptError, DatasetMissingError
    from rl_pipeline.datasets import get_dataset
    from rl_pipeline.settings import PipelineSettings
    from rl_pipeline.tasks import locate_or_download

    configure_cli_logging()  # rl_pipeline's JSON logs -> stderr -> this task's log
    spec = get_dataset(dataset or None)
    settings = PipelineSettings()
    target = Path(dataset_dir) if dataset_dir else settings.dataset_dir_for(spec.subdir)
    download = allow_download.strip().lower() != "false"
    nearest = target
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    params = {"dataset": spec.key, "dataset_dir": str(target)}
    if not download or os.access(nearest, os.W_OK):
        return locate_or_download({**params, "allow_download": download})
    try:
        return locate_or_download({**params, "allow_download": False})
    except (DatasetMissingError, DatasetCorruptError) as exc:
        fallback = settings.lake_downloads_dir / spec.subdir
        logging.getLogger("rl_pipeline.dag").warning(
            "dataset dir is read-only and has no verified copy: using the lake download dir",
            extra={
                "dataset": spec.key,
                "dataset_dir": str(target),
                "download_dir": str(fallback),
                "reason": str(exc),
            },
        )
        return locate_or_download(
            {"dataset": spec.key, "dataset_dir": str(fallback), "allow_download": True}
        )


@task.external_python(
    task_id="validate_raw_schema",
    execution_timeout=timedelta(minutes=10),
    **EXTERNAL,
)
def validate_raw_schema(dataset: dict[str, Any]) -> dict[str, Any]:
    """Check the dataset's raw header (names and order). Raises (fails the DAG) on any mismatch."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.tasks import validate_raw_schema as run

    configure_cli_logging()
    return run(dataset)


@task.external_python(
    task_id="convert_to_parquet",
    execution_timeout=timedelta(minutes=45),
    **EXTERNAL,
)
def convert_to_parquet(dataset: dict[str, Any]) -> dict[str, Any]:
    """Stream the .csv.gz with DuckDB into per-language Parquet under RL_LAKE_DIR."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.tasks import convert_to_parquet as run

    configure_cli_logging()
    return run(dataset)


@task.external_python(
    task_id="load_raw_s3",
    execution_timeout=timedelta(minutes=30),
    **EXTERNAL,
)
def load_raw_s3(conversion: dict[str, Any]) -> dict[str, Any]:
    """Upload Parquet + _manifest.json to s3://<bucket>/raw/travistorrent/version=<md5>/."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.tasks import load_raw_s3 as run

    configure_cli_logging()
    return run(conversion)


# ---------------------------------------------------------------- M3a: dbt (ADR-0006)
@task.external_python(task_id="dbt_deps", execution_timeout=timedelta(minutes=10), **DBT)
def dbt_deps(loaded: dict[str, Any], airflow_run_id: str = "") -> dict[str, Any]:
    """``dbt deps`` for data/dbt. Starts the dbt context: dataset + version from load_raw_s3,
    run_id = this DAG run's id (sanitised to a path-safe segment by rl_pipeline)."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dbt_runner import skip_if_not_applicable
    from rl_pipeline.tasks import dbt_deps as run

    configure_cli_logging()
    with skip_if_not_applicable():
        return run(
            {"dataset": loaded["dataset"], "version": loaded["version"], "run_id": airflow_run_id}
        )


@task.external_python(task_id="dbt_build_staging", execution_timeout=timedelta(minutes=30), **DBT)
def dbt_build_staging(context: dict[str, Any]) -> dict[str, Any]:
    """Source tests + staging models (DuckDB file under RL_LAKE_DIR/dbt) + their tests."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dbt_runner import skip_if_not_applicable
    from rl_pipeline.tasks import dbt_build_staging as run

    configure_cli_logging()
    with skip_if_not_applicable():
        return run(context)


@task.external_python(
    task_id="dbt_build_intermediate", execution_timeout=timedelta(minutes=30), **DBT
)
def dbt_build_intermediate(context: dict[str, Any]) -> dict[str, Any]:
    """Intermediate models (naive history, change profile, timing) + their tests."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dbt_runner import skip_if_not_applicable
    from rl_pipeline.tasks import dbt_build_intermediate as run

    configure_cli_logging()
    with skip_if_not_applicable():
        return run(context)


@task.external_python(task_id="dbt_build_marts", execution_timeout=timedelta(minutes=30), **DBT)
def dbt_build_marts(context: dict[str, Any]) -> dict[str, Any]:
    """Marts as Parquet: s3://<bucket>/marts/<model>/run_id=<run_id>/ + their tests."""
    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dbt_runner import skip_if_not_applicable
    from rl_pipeline.tasks import dbt_build_marts as run

    configure_cli_logging()
    with skip_if_not_applicable():
        return run(context)


@task.external_python(
    task_id="build_feature_snapshot",
    execution_timeout=timedelta(minutes=30),
    trigger_rule=TriggerRule.NONE_FAILED,
    **DBT,
)
def build_feature_snapshot(context: dict[str, Any] | None) -> dict[str, Any]:
    """dbt ml/ models: mart_build_inputs, split_cutpoints and mart_build_features (rl_core
    features, chronological split) -> s3://<bucket>/features/release_features/schema=<v>/
    run_id=<run_id>/ + _manifest.json. Runs after the export placeholder (NONE_FAILED); when
    dbt_build_marts was skipped (no dbt for this dataset) there is no context: skip too."""
    import sys

    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dbt_runner import SKIP_EXIT_CODE, skip_if_not_applicable
    from rl_pipeline.tasks import build_feature_snapshot as run

    configure_cli_logging()
    if not context:
        sys.stderr.write("build_feature_snapshot: no dbt context upstream (skipped)\n")
        raise SystemExit(SKIP_EXIT_CODE)
    with skip_if_not_applicable():
        return run(context)


# ---------------------------------------------------------------- M3/M4: placeholders
def _placeholder(task_id: str, milestone: str) -> Any:
    @task(
        task_id=task_id,
        trigger_rule=TriggerRule.NONE_FAILED,
        retries=0,
        execution_timeout=timedelta(minutes=1),
    )
    def _not_implemented() -> None:
        raise AirflowSkipException(
            f"{task_id}: implemented in {milestone} (placeholder, not a success)"
        )

    return _not_implemented()


@task.short_circuit(
    task_id="training_enabled",
    trigger_rule=TriggerRule.NONE_FAILED,
    retries=0,
    execution_timeout=timedelta(minutes=1),
)
def training_enabled(params: dict[str, Any] | None = None) -> bool:
    """Gate the training tasks on params.skip_training (default true until M4)."""
    skip = bool((params or {}).get("skip_training", True))
    log.info("params.skip_training=%s -> training tasks %s", skip, "skipped" if skip else "run")
    return not skip


# ---------------------------------------------------------------- DAG
@dag(
    dag_id=DAG_ID,
    schedule=None,
    catchup=False,
    max_active_runs=1,  # tasks share the /lake staging dir and the S3 version prefix
    dagrun_timeout=timedelta(hours=3),
    default_args=DEFAULT_ARGS,
    tags=["releaselens", "historical", "m2", "m2b", "m3a"],
    params={
        "dataset": Param(
            DEFAULT_DATASET,
            type="string",
            enum=list(DATASETS),
            description="Registered dataset (rl_pipeline.datasets); default: the primary "
            "TravisTorrent 5_3_2016 release (ADR-0017).",
        ),
        "skip_training": Param(
            True,
            type="boolean",
            description="Skip train_models/evaluate_and_register (default true until M4).",
        ),
        "dataset_dir": Param(
            None,
            type=["null", "string"],
            description="Override <RL_DATASETS_DIR>/<dataset subdir> (the directory holding the "
            "dataset's .csv.gz) for this run.",
        ),
        "allow_download": Param(
            True,
            type="boolean",
            description="Download from Figshare when the file is missing/corrupt (false: fail).",
        ),
    },
    doc_md=__doc__,
)
def historical_pipeline() -> None:
    # At definition time these are XComArgs; at run time each task receives the upstream dict.
    dataset: Any = download_or_locate_dataset(
        dataset="{{ params.dataset }}",
        dataset_dir="{{ params.dataset_dir or '' }}",
        allow_download="{{ params.allow_download }}",
    )
    validated: Any = validate_raw_schema(dataset)
    conversion: Any = convert_to_parquet(dataset)
    loaded: Any = load_raw_s3(conversion)

    # "run_id" is an Airflow context key and cannot be a task parameter name.
    deps: Any = dbt_deps(loaded, airflow_run_id="{{ run_id }}")
    staging: Any = dbt_build_staging(deps)
    intermediate: Any = dbt_build_intermediate(staging)
    marts: Any = dbt_build_marts(intermediate)
    export = _placeholder("export_analytics_to_postgres", "M3b")
    features: Any = build_feature_snapshot(marts)
    m4 = [_placeholder(task_id, "M4") for task_id in PLACEHOLDERS_M4]

    # convert consumes the located dataset dict but must only start once validation passed.
    chain(validated, conversion)
    # The spec's single chain: the export (M3b) sits between the marts and the feature snapshot.
    chain(marts, export, features, training_enabled(), *m4)


historical_pipeline()
