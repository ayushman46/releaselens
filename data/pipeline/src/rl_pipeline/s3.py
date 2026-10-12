"""S3-compatible object storage (pgsty/silo locally, ADR-0009): buckets + idempotent uploads.

Idempotency: every object carries its sha256 in user metadata (``x-amz-meta-sha256``). An upload
is skipped when an object with the same key already exists and its metadata sha256 matches. The
version manifest (``_manifest.json``) is written last, so its presence means the version is
complete. Before it is written, the version prefix must hold exactly the expected keys: a stray
object (e.g. from an older layout) would be picked up by any ``lang=*/*.parquet`` reader, so it
fails the load instead of being ignored.

CLI::

    python -m rl_pipeline.s3 ensure-buckets [--wait-seconds 60]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from rl_pipeline.hashing import sha256_file
from rl_pipeline.settings import PipelineSettings

log = logging.getLogger("rl_pipeline.s3")

RAW_PREFIX = "raw/travistorrent"
_NOT_FOUND = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


class UploadVerificationError(RuntimeError):
    pass


def make_client(settings: PipelineSettings) -> Any:
    """boto3 S3 client with path-style addressing (works with silo/MinIO/SeaweedFS/AWS)."""
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        ),
    )


def _is_not_found(exc: ClientError) -> bool:
    err = exc.response.get("Error", {})
    status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return str(err.get("Code", "")) in _NOT_FOUND or status == "404"


def ensure_buckets(client: Any, buckets: Iterable[str]) -> dict[str, str]:
    """Create missing buckets. Idempotent: returns ``{bucket: "created" | "exists"}``."""
    result: dict[str, str] = {}
    for bucket in buckets:
        try:
            client.head_bucket(Bucket=bucket)
            result[bucket] = "exists"
            continue
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        try:
            client.create_bucket(Bucket=bucket)
            result[bucket] = "created"
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
            result[bucket] = "exists"
    log.info("buckets ensured", extra={"buckets": result})
    return result


def remote_sha256(client: Any, bucket: str, key: str) -> str | None:
    """The object's metadata sha256, or ``None`` if the object does not exist."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _is_not_found(exc):
            return None
        raise
    meta = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}
    return meta.get("sha256", "")


def upload_file_idempotent(
    client: Any, bucket: str, key: str, path: Path, sha256: str, content_type: str
) -> bool:
    """Upload unless the same key already holds the same sha256. Returns True if uploaded."""
    if remote_sha256(client, bucket, key) == sha256:
        return False
    client.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"Metadata": {"sha256": sha256}, "ContentType": content_type},
    )
    return True


def put_bytes_idempotent(
    client: Any, bucket: str, key: str, body: bytes, sha256: str, content_type: str
) -> bool:
    if remote_sha256(client, bucket, key) == sha256:
        return False
    client.put_object(
        Bucket=bucket, Key=key, Body=body, Metadata={"sha256": sha256}, ContentType=content_type
    )
    return True


def list_objects(client: Any, bucket: str, prefix: str) -> dict[str, int]:
    """``{key: size}`` for every object under ``prefix`` (paginated)."""
    out: dict[str, int] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            out[str(obj["Key"])] = int(obj["Size"])
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def version_prefix(version: str) -> str:
    return f"{RAW_PREFIX}/version={version}"


def _verify_listing(
    client: Any,
    bucket: str,
    prefix: str,
    objects: list[dict[str, Any]],
    allowed_extra: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Every expected key is listed with its size, and no unexpected key exists under prefix."""
    listing = list_objects(client, bucket, prefix + "/")
    for obj in objects:
        if listing.get(obj["key"]) != obj["size"]:
            raise UploadVerificationError(
                f"s3://{bucket}/{obj['key']} listed size {listing.get(obj['key'])} != {obj['size']}"
            )
    unexpected = sorted(set(listing) - {o["key"] for o in objects} - set(allowed_extra))
    if unexpected:
        raise UploadVerificationError(
            f"s3://{bucket}/{prefix}/ holds objects that are not part of this version: "
            f"{unexpected[:20]} (remove them; readers glob this prefix)"
        )


def load_version(client: Any, bucket: str, conversion: dict[str, Any]) -> dict[str, Any]:
    """Upload a converted version (Parquet files first, ``_manifest.json`` last) idempotently."""
    version = str(conversion["version"])
    local_dir = Path(conversion["local_dir"])
    prefix = version_prefix(version)
    manifest_path = local_dir / "_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"{manifest_path} missing: run convert_to_parquet first")

    for f in conversion["files"]:
        rel = str(f["path"])
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError(f"refusing unsafe relative path {rel!r}")
    # The XCom dict and the manifest on disk must describe the same files; otherwise the uploaded
    # manifest would claim objects that were never uploaded (or vice versa).
    body = manifest_path.read_bytes()  # read once: the bytes checked are the bytes uploaded
    manifest_sha = hashlib.sha256(body).hexdigest()
    local_manifest = json.loads(body)
    claimed = sorted((str(f["path"]), str(f["sha256"])) for f in conversion["files"])
    on_disk = sorted((str(f["path"]), str(f["sha256"])) for f in local_manifest.get("files", []))
    if local_manifest.get("version") != version or claimed != on_disk:
        raise UploadVerificationError(
            f"conversion result does not match {manifest_path} (version or files differ); "
            "re-run convert_to_parquet"
        )

    objects: list[dict[str, Any]] = []
    uploaded = skipped = 0
    for f in sorted(conversion["files"], key=lambda f: str(f["path"])):
        rel = str(f["path"])
        path = local_dir / rel
        actual = sha256_file(path)
        if actual != f["sha256"]:
            raise UploadVerificationError(
                f"{path} sha256 {actual} differs from the conversion result {f['sha256']}"
            )
        key = f"{prefix}/{rel}"
        did = upload_file_idempotent(
            client, bucket, key, path, actual, "application/vnd.apache.parquet"
        )
        uploaded += did
        skipped += not did
        objects.append({"key": key, "size": path.stat().st_size, "sha256": actual})

    manifest_key = f"{prefix}/_manifest.json"

    # Verify the data objects (and that nothing else lives under the prefix) before the manifest
    # marks the version complete.
    _verify_listing(client, bucket, prefix, objects, allowed_extra={manifest_key})
    did = put_bytes_idempotent(client, bucket, manifest_key, body, manifest_sha, "application/json")
    uploaded += did
    skipped += not did
    objects.append({"key": manifest_key, "size": len(body), "sha256": manifest_sha})
    _verify_listing(client, bucket, prefix, objects)
    result = {
        "bucket": bucket,
        "prefix": prefix,
        "objects": objects,
        "skipped": skipped,
        "uploaded": uploaded,
    }
    log.info("version loaded", extra={k: v for k, v in result.items() if k != "objects"})
    return result


# ---------------------------------------------------------------- CLI
def _ensure_with_wait(settings: PipelineSettings, wait_seconds: float) -> dict[str, str]:
    client = make_client(settings)
    buckets = [settings.s3_bucket_data, settings.s3_bucket_mlflow]
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return ensure_buckets(client, buckets)
        except (BotoConnectionError, HTTPClientError, OSError) as exc:  # not up yet
            if time.monotonic() >= deadline:
                raise
            log.info("waiting for object store", extra={"error": str(exc)})
            time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rl_pipeline.s3")
    sub = parser.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure-buckets", help="create the data + mlflow buckets if missing")
    ensure.add_argument("--wait-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)

    from rl_pipeline.__main__ import configure_cli_logging

    configure_cli_logging()
    settings = PipelineSettings()
    if args.command == "ensure-buckets":
        result = _ensure_with_wait(settings, args.wait_seconds)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
