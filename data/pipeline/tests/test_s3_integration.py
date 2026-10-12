"""Real S3 round-trip against pgsty/silo (ADR-0009) in a throwaway testcontainer.

Run with: DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true \
          uv run pytest -m integration data/pipeline
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import SecretStr

from rl_pipeline.s3 import ensure_buckets, list_objects, load_version, make_client
from rl_pipeline.settings import PipelineSettings

pytestmark = pytest.mark.integration

SILO_IMAGE = "pgsty/silo:RELEASE.2026-09-16T00-00-00Z"
USER, PASSWORD = "rlens-test", "rlens-test-secret"  # throwaway container credentials


@pytest.fixture(scope="module")
def silo_client() -> Any:
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(SILO_IMAGE)
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", USER)
        .with_env("MINIO_ROOT_PASSWORD", PASSWORD)
        .with_exposed_ports(9000)
    )
    with container:
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        settings = PipelineSettings(
            s3_endpoint=endpoint, s3_access_key=USER, s3_secret_key=SecretStr(PASSWORD)
        )
        client = make_client(settings)
        deadline = time.monotonic() + 60
        while True:
            try:
                client.list_buckets()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield client


def test_ensure_buckets_idempotent_on_silo(silo_client: Any) -> None:
    buckets = ["releaselens-data", "releaselens-mlflow"]
    first = ensure_buckets(silo_client, buckets)
    assert set(first.values()) <= {"created", "exists"}
    assert ensure_buckets(silo_client, buckets) == dict.fromkeys(buckets, "exists")
    names = {b["Name"] for b in silo_client.list_buckets()["Buckets"]}
    assert set(buckets) <= names


def test_upload_twice_second_run_skips_all(silo_client: Any, converted: dict[str, Any]) -> None:
    bucket = "releaselens-data"
    ensure_buckets(silo_client, [bucket])
    first = load_version(silo_client, bucket, converted)
    n = len(converted["files"]) + 1
    assert first["uploaded"] == n
    assert first["skipped"] == 0

    second = load_version(silo_client, bucket, converted)
    assert second["uploaded"] == 0
    assert second["skipped"] == n
    assert second["objects"] == first["objects"]

    listing = list_objects(silo_client, bucket, first["prefix"] + "/")
    assert listing == {o["key"]: o["size"] for o in first["objects"]}
    for obj in first["objects"]:
        head = silo_client.head_object(Bucket=bucket, Key=obj["key"])
        assert head["Metadata"]["sha256"] == obj["sha256"]
