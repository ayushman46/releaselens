"""Publish ``findings.json`` to ``s3://<bucket>/reports/analytics/run_id=<run_id>/findings.json``.

Idempotent like the pipeline's uploads: the object carries ``x-amz-meta-sha256`` and an upload is
skipped when the key already holds the same bytes.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from rl_analysis.settings import AnalysisSettings

REPORT_PREFIX: Final = "reports/analytics"


def report_key(run_id: str) -> str:
    return f"{REPORT_PREFIX}/run_id={run_id}/findings.json"


def make_client(settings: AnalysisSettings) -> Any:
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


def upload_report(client: Any, bucket: str, run_id: str, body: bytes) -> dict[str, Any]:
    """Put the report unless the key already holds the same sha256."""
    key = report_key(run_id)
    digest = hashlib.sha256(body).hexdigest()
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        existing = (head.get("Metadata") or {}).get("sha256")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        existing = None
    location = f"s3://{bucket}/{key}"
    if existing == digest:
        return {"location": location, "sha256": digest, "bytes": len(body), "uploaded": False}
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        Metadata={"sha256": digest},
    )
    return {"location": location, "sha256": digest, "bytes": len(body), "uploaded": True}
