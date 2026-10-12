from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rl_pipeline.s3 import (
    UploadVerificationError,
    ensure_buckets,
    list_objects,
    load_version,
    remote_sha256,
    upload_file_idempotent,
)
from rl_pipeline.testing import FakeS3Client

BUCKET = "releaselens-data"


@pytest.fixture
def conversion(converted: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """A private copy of the session conversion (tests may mutate files)."""
    local = tmp_path / Path(converted["local_dir"]).name
    shutil.copytree(converted["local_dir"], local)
    return {**converted, "local_dir": str(local)}


def test_ensure_buckets_is_idempotent() -> None:
    client = FakeS3Client()
    assert ensure_buckets(client, ["a", "b"]) == {"a": "created", "b": "created"}
    assert ensure_buckets(client, ["a", "b"]) == {"a": "exists", "b": "exists"}
    assert [c[0] for c in client.calls] == ["create_bucket", "create_bucket"]


def test_ensure_buckets_propagates_other_errors() -> None:
    class Forbidden(FakeS3Client):
        def head_bucket(self, Bucket: str) -> dict[str, Any]:  # noqa: N803
            raise ClientError({"Error": {"Code": "403"}}, "HeadBucket")

    with pytest.raises(ClientError):
        ensure_buckets(Forbidden(), ["a"])


def test_upload_twice_skips_everything(conversion: dict[str, Any]) -> None:
    client = FakeS3Client(page_size=2)  # forces list_objects_v2 pagination
    ensure_buckets(client, [BUCKET])
    first = load_version(client, BUCKET, conversion)
    n_files = len(conversion["files"])
    assert first["uploaded"] == n_files + 1
    assert first["skipped"] == 0
    prefix = f"raw/travistorrent/version={conversion['version']}"
    assert first["prefix"] == prefix
    keys = [o["key"] for o in first["objects"]]
    assert keys[-1] == f"{prefix}/_manifest.json"
    assert f"{prefix}/lang=java/part-00000.parquet" in keys
    # manifest is the very last write
    assert client.mutations()[-1] == ("put_object", BUCKET, f"{prefix}/_manifest.json")
    for obj in first["objects"]:
        assert remote_sha256(client, BUCKET, obj["key"]) == obj["sha256"]

    writes_before = len(client.mutations())
    second = load_version(client, BUCKET, conversion)
    assert second["uploaded"] == 0
    assert second["skipped"] == n_files + 1
    assert len(client.mutations()) == writes_before
    assert second["objects"] == first["objects"]
    assert list_objects(client, BUCKET, prefix + "/") == {
        o["key"]: o["size"] for o in first["objects"]
    }


def test_changed_object_metadata_is_reuploaded(conversion: dict[str, Any]) -> None:
    client = FakeS3Client()
    ensure_buckets(client, [BUCKET])
    first = load_version(client, BUCKET, conversion)
    key = first["objects"][0]["key"]
    client.buckets[BUCKET][key].metadata["sha256"] = "stale"
    again = load_version(client, BUCKET, conversion)
    assert again["uploaded"] == 1
    assert again["skipped"] == len(first["objects"]) - 1


def test_local_file_drift_is_refused(conversion: dict[str, Any]) -> None:
    client = FakeS3Client()
    ensure_buckets(client, [BUCKET])
    target = Path(conversion["local_dir"]) / conversion["files"][0]["path"]
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(UploadVerificationError, match="differs from the conversion result"):
        load_version(client, BUCKET, conversion)
    assert client.mutations() == []


def test_unsafe_relative_path_is_refused(conversion: dict[str, Any]) -> None:
    bad = {**conversion, "files": [{**conversion["files"][0], "path": "../escape.parquet"}]}
    with pytest.raises(ValueError, match="unsafe"):
        load_version(FakeS3Client(), BUCKET, bad)


def test_missing_manifest_is_refused(conversion: dict[str, Any]) -> None:
    (Path(conversion["local_dir"]) / "_manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        load_version(FakeS3Client(), BUCKET, conversion)


def test_upload_file_idempotent_helper(tmp_path: Path) -> None:
    client = FakeS3Client()
    ensure_buckets(client, [BUCKET])
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    assert upload_file_idempotent(client, BUCKET, "k", f, "s1", "application/octet-stream")
    assert not upload_file_idempotent(client, BUCKET, "k", f, "s1", "application/octet-stream")
    assert upload_file_idempotent(client, BUCKET, "k", f, "s2", "application/octet-stream")
    assert remote_sha256(client, BUCKET, "missing") is None


def test_stray_object_under_version_prefix_fails_before_manifest(
    conversion: dict[str, Any],
) -> None:
    client = FakeS3Client()
    ensure_buckets(client, [BUCKET])
    prefix = f"raw/travistorrent/version={conversion['version']}"
    stray = f"{prefix}/lang=java/part-00001.parquet"  # e.g. left over from an older layout
    client.put_object(Bucket=BUCKET, Key=stray, Body=b"old", Metadata={})
    with pytest.raises(UploadVerificationError, match="not part of this version"):
        load_version(client, BUCKET, conversion)
    assert f"{prefix}/_manifest.json" not in client.buckets[BUCKET]  # never marked complete


def test_conversion_dict_must_match_local_manifest(conversion: dict[str, Any]) -> None:
    partial = {**conversion, "files": conversion["files"][:-1]}  # e.g. a stale/partial XCom
    client = FakeS3Client()
    ensure_buckets(client, [BUCKET])
    with pytest.raises(UploadVerificationError, match="does not match"):
        load_version(client, BUCKET, partial)
    assert client.mutations() == []
