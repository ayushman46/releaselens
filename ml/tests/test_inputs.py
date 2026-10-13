"""Model-input contract: all-null features are dropped; mart files must match their manifest."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest

from rl_ml import card
from rl_ml.data import MartSchemaError, S3Settings, list_feature_runs, load_mart, mart_prefix


def test_all_null_features_are_dropped_from_model_inputs(trained_run) -> None:
    cov = trained_run.results["feature_coverage"]
    dropped = set(cov["dropped_from_model_inputs"])
    assert {"num_commits_in_push", "description_complexity"} <= dropped
    for cand in trained_run.outcome.candidates:
        assert not set(cand.features) & dropped
    assert not set(trained_run.bundle.features) & dropped
    text = card.render_results({"failed_or_errored": trained_run.results})
    assert "dropped from all model inputs" in text


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def list_objects_v2(self, Bucket: str, Prefix: str, **_: Any) -> dict[str, Any]:
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {
            "Contents": [
                {"Key": k, "Size": len(self.objects[k]), "LastModified": stamp} for k in keys
            ]
        }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        return {"Body": io.BytesIO(self.objects[Key])}


def _store(synthetic_mart, *, corrupt: bool = False) -> FakeS3:
    buf = io.BytesIO()
    pl.from_pandas(synthetic_mart.frame.drop(columns=["status"])).write_parquet(buf)
    body = buf.getvalue()
    manifest = {
        "feature_schema_version": "1.0.0",
        "dataset_version": "4713d8b73b667a2c375b89c20f531fc0",
        "file": {
            "path": "part-00000.parquet",
            "rows": len(synthetic_mart.frame),
            "sha256": "0" * 64 if corrupt else hashlib.sha256(body).hexdigest(),
        },
    }
    pre = mart_prefix("r1")
    return FakeS3(
        {pre + "part-00000.parquet": body, pre + "_manifest.json": json.dumps(manifest).encode()}
    )


def test_load_mart_verifies_the_manifest(synthetic_mart) -> None:
    store = _store(synthetic_mart)
    settings = S3Settings(bucket_data="b")
    assert list_feature_runs(store, "b") == ["r1"]
    m = load_mart("r1", client=store, settings=settings)
    assert len(m.frame) == len(synthetic_mart.frame)
    assert m.files[0]["rows"] == len(synthetic_mart.frame)
    assert "manifest verified (sha256 + rows)" in m.notes
    with pytest.raises(MartSchemaError, match="manifest"):
        load_mart("r1", client=_store(synthetic_mart, corrupt=True), settings=settings)
