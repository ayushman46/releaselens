"""Dataset registry: keys, sources, resolution rules and spec self-consistency."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from rl_core.features.change import BUILD_INPUT_FIELDS
from rl_pipeline.convert import _settings_block
from rl_pipeline.datasets import (
    DATASETS,
    DEFAULT_DATASET,
    PRIMARY,
    SECONDARY,
    TT_5_3_2016,
    TT_FINAL_2017,
    UnknownDatasetError,
    dataset_for_schema,
    dataset_for_version,
    get_dataset,
    resolve,
)

# Published converter block of version 5d359345fecf0bf47be6edb10b5aae58 (read from the
# releaselens-data bucket on 2026-10-13). The secondary spec must keep reproducing it, or the
# cached conversion and the S3 objects would be invalidated.
PUBLISHED_2017_CONVERTER = {
    "compression": "zstd",
    "converter_version": "1",
    "duckdb_version": "1.5.6",
    "order_by": ["tr_build_id", "tr_job_id"],
    "partition_by": "gh_lang",
    "row_group_size": 122880,
    "schema_fingerprint": "5dd3f6d6ecc47f7b07eb7f29eb04aadf3d27b271511b9016ac4501de55423fe4",
}


def test_registry_keys_and_default() -> None:
    assert set(DATASETS) == {"travistorrent-5-3-2016", "travistorrent-final-2017-01-25"}
    assert DEFAULT_DATASET == PRIMARY == "travistorrent-5-3-2016"
    assert SECONDARY == "travistorrent-final-2017-01-25"
    assert get_dataset() is TT_5_3_2016
    assert get_dataset("") is TT_5_3_2016
    assert get_dataset(SECONDARY) is TT_FINAL_2017


def test_real_sources() -> None:
    src = TT_5_3_2016.source
    assert (src.filename, src.size, src.md5) == (
        "travistorrent_5_3_2016.csv.gz",
        81_902_127,
        "4713d8b73b667a2c375b89c20f531fc0",
    )
    assert src.url == "https://ndownloader.figshare.com/files/34303127"
    assert TT_5_3_2016.subdir == "travistorrent-figshare-19314170-5_3_2016"
    old = TT_FINAL_2017.source
    assert (old.filename, old.size, old.md5) == (
        "final-2017-01-25.csv.gz",
        264_607_174,
        "5d359345fecf0bf47be6edb10b5aae58",
    )
    assert TT_FINAL_2017.subdir == "travistorrent-figshare-19314170"
    assert src.doi == old.doi == "10.6084/m9.figshare.19314170"
    assert src.license == old.license == "CC BY 4.0"


def test_secondary_spec_reproduces_published_converter_block() -> None:
    assert _settings_block(TT_FINAL_2017) == PUBLISHED_2017_CONVERTER
    assert TT_FINAL_2017.manifest_extras is False
    assert TT_FINAL_2017.timestamp_semantics == "UTC wall-clock stored as TIMESTAMP (no zone)"


def test_primary_conversion_keys() -> None:
    assert TT_5_3_2016.sort_key == ("tr_build_id", "tr_job_id", "row")
    assert TT_5_3_2016.unique_column == "row"
    assert TT_5_3_2016.started_at_column == "tr_started_at"
    assert dict(TT_5_3_2016.patterns) == {"tr_prev_build": r"[+-]?[0-9]+(\.0+)?"}
    assert TT_5_3_2016.pattern_for("tr_prev_build", "BIGINT") == r"[+-]?[0-9]+(\.0+)?"
    assert TT_5_3_2016.pattern_for("tr_build_id", "BIGINT") == r"[+-]?[0-9]+"
    assert TT_5_3_2016.pattern_for("gh_lang", "VARCHAR") is None


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(UnknownDatasetError, match="registered"):
        get_dataset("travistorrent-8-2-2017")


def test_resolve_rules() -> None:
    v17, v16 = TT_FINAL_2017.source.md5, TT_5_3_2016.source.md5
    assert resolve(None) is TT_5_3_2016
    assert resolve({"version": "0" * 32}) is TT_5_3_2016  # unregistered md5 -> default
    assert resolve({"version": v17}) is TT_FINAL_2017  # registered md5 picks its release
    assert resolve({"dataset": SECONDARY, "version": "0" * 32}) is TT_FINAL_2017
    assert resolve({"dataset": PRIMARY, "version": v16}) is TT_5_3_2016
    with pytest.raises(UnknownDatasetError, match="contradicts"):
        resolve({"dataset": PRIMARY, "version": v17})
    assert dataset_for_version(v16) is TT_5_3_2016
    assert dataset_for_version(None) is None
    assert dataset_for_schema(TT_FINAL_2017.names) is TT_FINAL_2017
    assert dataset_for_schema(("a", "b")) is None


def test_build_input_maps_cover_rl_core_inputs() -> None:
    for spec in DATASETS.values():
        assert tuple(m.field for m in spec.build_input_map) == BUILD_INPUT_FIELDS
        for m in spec.build_input_map:
            assert (m.column is None) == (m.status == "none")


def test_summary_is_json() -> None:
    for spec in DATASETS.values():
        s = spec.summary()
        assert json.loads(json.dumps(s)) == s
        assert s["default"] is (spec.key == DEFAULT_DATASET)


def test_spec_validation_rejects_inconsistent_specs() -> None:
    with pytest.raises(ValueError, match="sort_key must end"):
        replace(TT_5_3_2016, sort_key=("tr_build_id", "tr_job_id"))
    with pytest.raises(ValueError, match="not a column"):
        replace(TT_5_3_2016, started_at_column="gh_build_started_at")
    with pytest.raises(ValueError, match="patterns for unknown columns"):
        replace(TT_5_3_2016, patterns={"nope": "x"})
    with pytest.raises(ValueError, match="column_docs"):
        replace(TT_5_3_2016, column_docs=TT_FINAL_2017.column_docs)


# ---------------------------------------------------------------- 2016 catalog tags (M2b rules)
POST_2016 = {
    "tr_status",
    "tr_duration",
    "tr_setup_time",
    "tr_analyzer",
    "tr_frameworks",
    "tr_tests_ok",
    "tr_tests_fail",
    "tr_tests_run",
    "tr_tests_skipped",
    "tr_failed_tests",
    "tr_testduration",
    "tr_purebuildduration",
    "tr_tests_ran",
    "tr_tests_failed",
    "tr_ci_latency",
}
UNCERTAIN_2016 = {
    "git_merged_with",
    "gh_num_issue_comments",
    "gh_num_commit_comments",
    "gh_num_pr_comments",
    "gh_description_complexity",
    "tr_lan",
}


def test_2016_availability_tags() -> None:
    docs = TT_5_3_2016.column_docs
    by_tag: dict[str, set[str]] = {}
    for doc in docs.values():
        by_tag.setdefault(doc.availability, set()).add(doc.name)
        assert doc.meaning.strip()
        assert doc.justification.strip()
    assert by_tag["post_build"] == POST_2016
    assert by_tag["uncertain"] == UNCERTAIN_2016
    assert by_tag["pre_build"] == set(TT_5_3_2016.names) - POST_2016 - UNCERTAIN_2016
    assert docs["tr_status"].role == "label"
    assert docs["tr_lan"].role == "identifier"
    assert docs["tr_prev_build"].role == "identifier"
    assert docs["tr_num_jobs"].availability == "pre_build"
    assert docs["gh_first_commit_created_at"].role == "excluded"
    for name in UNCERTAIN_2016 - {"tr_lan"}:
        assert docs[name].role == "excluded"
    for doc in docs.values():
        if doc.role in {"identifier", "context"}:
            assert "not a model feature" in doc.justification
        if doc.role == "feature_candidate":
            assert doc.availability == "pre_build"
