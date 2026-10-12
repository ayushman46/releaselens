"""historical_pipeline: TravisTorrent (Figshare) to Parquet to S3, then dbt and ML (spec §7.2).

Implemented in M2:   download_or_locate_dataset -> validate_raw_schema -> convert_to_parquet
                     -> load_raw_s3
Placeholders:        dbt_deps -> dbt_build_staging -> dbt_build_intermediate -> dbt_build_marts
                     -> export_analytics_to_postgres -> build_feature_snapshot   (M3)
                     -> training_enabled (short-circuit on params.skip_training)
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
    them upstream_failed. Real implementations go back to the default ALL_SUCCESS.
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

PLACEHOLDERS_M3 = (
    "dbt_deps",
    "dbt_build_staging",
    "dbt_build_intermediate",
    "dbt_build_marts",
    "export_analytics_to_postgres",
    "build_feature_snapshot",
)
PLACEHOLDERS_M4 = ("train_models", "evaluate_and_register")


# ---------------------------------------------------------------- M2: implemented tasks
@task.external_python(
    task_id="download_or_locate_dataset",
    execution_timeout=timedelta(minutes=30),
    **EXTERNAL,
)
def download_or_locate_dataset(
    dataset_dir: str = "", allow_download: str = "True"
) -> dict[str, Any]:
    """Find the Figshare file (or download it), verify its md5. Returns path/md5/size/version.

    Arguments arrive as rendered Jinja strings (op_kwargs templates), hence the str types.

    Compose mounts the host's dataset dir read-only, so a download (or the re-acquisition of a
    corrupt copy) can never happen there. When the dataset dir is not writable and holds no
    verified copy, the file is downloaded to ``<RL_LAKE_DIR>/downloads/<source name>`` instead
    (a writable volume); the read-only copy is never modified.
    """
    import logging
    import os
    from pathlib import Path

    from rl_pipeline.__main__ import configure_cli_logging
    from rl_pipeline.dataset import (
        TRAVISTORRENT_FIGSHARE,
        DatasetCorruptError,
        DatasetMissingError,
    )
    from rl_pipeline.settings import PipelineSettings
    from rl_pipeline.tasks import locate_or_download

    configure_cli_logging()  # rl_pipeline's JSON logs -> stderr -> this task's log
    settings = PipelineSettings()
    target = Path(dataset_dir) if dataset_dir else settings.dataset_dir
    download = allow_download.strip().lower() != "false"
    nearest = target
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    if not download or os.access(nearest, os.W_OK):
        return locate_or_download({"dataset_dir": str(target), "allow_download": download})
    try:
        return locate_or_download({"dataset_dir": str(target), "allow_download": False})
    except (DatasetMissingError, DatasetCorruptError) as exc:
        fallback = settings.lake_dir / "downloads" / TRAVISTORRENT_FIGSHARE.name
        logging.getLogger("rl_pipeline.dag").warning(
            "dataset dir is read-only and has no verified copy: using the lake download dir",
            extra={"dataset_dir": str(target), "download_dir": str(fallback), "reason": str(exc)},
        )
        return locate_or_download({"dataset_dir": str(fallback), "allow_download": True})


@task.external_python(
    task_id="validate_raw_schema",
    execution_timeout=timedelta(minutes=10),
    **EXTERNAL,
)
def validate_raw_schema(dataset: dict[str, Any]) -> dict[str, Any]:
    """Check the 66-column raw schema. Raises (fails the DAG) on any mismatch."""
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
            f"{task_id}: implemented in {milestone} (placeholder in M2, not a success)"
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
    tags=["releaselens", "historical", "m2"],
    params={
        "skip_training": Param(
            True,
            type="boolean",
            description="Skip train_models/evaluate_and_register (default true until M4).",
        ),
        "dataset_dir": Param(
            None,
            type=["null", "string"],
            description="Override RL_DATASET_DIR (directory holding final-2017-01-25.csv.gz).",
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
        dataset_dir="{{ params.dataset_dir or '' }}",
        allow_download="{{ params.allow_download }}",
    )
    validated: Any = validate_raw_schema(dataset)
    conversion: Any = convert_to_parquet(dataset)
    loaded: Any = load_raw_s3(conversion)

    m3 = [_placeholder(task_id, "M3") for task_id in PLACEHOLDERS_M3]
    m4 = [_placeholder(task_id, "M4") for task_id in PLACEHOLDERS_M4]

    # convert consumes the located dataset dict but must only start once validation passed.
    chain(validated, conversion)
    chain(loaded, *m3, training_enabled(), *m4)


historical_pipeline()
