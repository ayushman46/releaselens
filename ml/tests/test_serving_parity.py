"""Training/serving parity on the REAL feature mart (spec §9).

For a deterministic sample of 200 labelled builds of feature-mart run ``m3-run1``, rebuild each
build's history from ``mart_build_inputs`` (every build of the project, canceled and unfinished
ones included, exactly the rows the offline batch saw) as the ``PastRun`` list the API would
load from ``ci_runs``, and call the serving function ``rl_core.features.online.compute_online``:

* on the full history, and
* on the bounded window the API actually loads (``bounded_history``, 200 most recent eligible
  runs + the latest start) with the exact totals,

Both must equal the mart's feature row (rtol 1e-9; ``None`` == null). Skipped (with the reason)
when the object store or the mart run is not available.
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Any

import numpy as np
import pytest

from rl_core.features.change import BUILD_INPUT_FIELDS
from rl_core.features.online import PastRun, bounded_history, compute_online
from rl_core.features.schema import ALL_FEATURE_NAMES, FEATURE_SCHEMA_VERSION

pytestmark = pytest.mark.integration

RUN_ID = "m3-run1"
FEATURES_KEY = (
    f"features/release_features/schema={FEATURE_SCHEMA_VERSION}/run_id={RUN_ID}/part-00000.parquet"
)
INPUTS_KEY = f"marts/mart_build_inputs/run_id={RUN_ID}/part-00000.parquet"
SAMPLE = 200
SEED = 20261013
SERVING_LIMIT = 200  # RL_RISK_HISTORY_LIMIT default


def _read(key: str) -> dict[str, list[Any]]:
    import pyarrow.parquet as pq

    from rl_ml.data import S3Settings, make_s3_client

    settings = S3Settings.from_env()
    try:
        body = make_s3_client(settings).get_object(Bucket=settings.bucket_data, Key=key)["Body"]
        table = pq.read_table(io.BytesIO(body.read()))
    except Exception as exc:  # no object store, no bucket, no such run
        pytest.skip(f"real mart not available (s3://{settings.bucket_data}/{key}): {exc}")
    return {name: table.column(name).to_pylist() for name in table.column_names}


@pytest.fixture(scope="module")
def marts() -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    return _read(FEATURES_KEY), _read(INPUTS_KEY)


def _fact(value: Any) -> float | int | bool | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value  # type: ignore[no-any-return]


def test_online_features_equal_the_training_mart(marts) -> None:
    feats, inputs = marts
    n_inputs = len(inputs["build_id"])
    by_project: dict[str, list[int]] = {}
    for i in sorted(range(n_inputs), key=lambda i: inputs["build_id"][i]):  # offline input order
        by_project.setdefault(inputs["project"][i], []).append(i)
    row_of = {b: i for i, b in enumerate(inputs["build_id"])}

    rng = np.random.default_rng(SEED)
    picks = sorted(rng.choice(len(feats["build_id"]), size=SAMPLE, replace=False).tolist())
    exercised_bound = 0
    for j in picks:
        build_id = feats["build_id"][j]
        me = row_of[build_id]
        t: datetime = inputs["started_at"][me]
        runs = [
            PastRun(
                started_at=inputs["started_at"][i],
                finished_at=inputs["finished_at"][i],
                failed=None if inputs["failed"][i] is None else bool(inputs["failed"][i]),
                duration_s=inputs["duration_s"][i],
            )
            for i in by_project[inputs["project"][me]]
            if i != me
        ]
        facts = {k: _fact(inputs[k][me]) for k in BUILD_INPUT_FIELDS}
        full = compute_online(facts, runs, t)
        window, totals = bounded_history(runs, t, SERVING_LIMIT)
        if totals.eligible > SERVING_LIMIT:
            exercised_bound += 1
        served = compute_online(facts, window, t, totals=totals)
        assert served == full, build_id
        for name in ALL_FEATURE_NAMES:
            got, want = full[name], feats[name][j]
            want = None if want is None or (isinstance(want, float) and math.isnan(want)) else want
            if got is None or want is None:
                assert got is None, (build_id, name, got, want)
                assert want is None, (build_id, name, got, want)
            else:
                assert got == pytest.approx(want, rel=1e-9, abs=1e-12), (build_id, name, got, want)
    # the sample must include builds whose history is longer than what the API loads
    assert exercised_bound > 0
