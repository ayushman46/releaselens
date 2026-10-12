"""Test helpers: a SYNTHETIC TravisTorrent-shaped fixture generator and an in-memory S3 fake.

**Nothing here is real data.** The fixture uses the real 66-column TravisTorrent 2017 header
(from :mod:`rl_pipeline.schema`); every value is made up by a seeded PRNG. Regenerate the
committed fixture with::

    uv run python -m rl_pipeline.testing \
        data/pipeline/tests/fixtures/travistorrent_synthetic_fixture.csv.gz

The output is byte-stable (fixed seed, gzip mtime 0); a unit test guards against drift between
this generator and the committed file. :class:`FakeS3Client` implements just the boto3 S3 calls
the pipeline uses, so S3 logic is unit-testable without a server or new dependencies.
"""

# ruff: noqa: N803, S311 - fake mirrors boto3 keyword casing; PRNG only makes synthetic data

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from rl_pipeline.schema import EXPECTED_NAMES

FIXTURE_NAME = "travistorrent_synthetic_fixture.csv.gz"
FREE_TEXT_MARKER = "SYNTHETIC_FREE_TEXT_MARKER"
SEED = 20261013

# (lang, project, build ids, jobs per build) — ids of different digit lengths test numeric order.
_PLAN: tuple[tuple[str, str, tuple[int, ...], tuple[int, ...]], ...] = (
    ("java", "synthetic-org/alpha", (99, 100, 1000, 1001, 20000), (2, 1, 3, 2, 3)),
    ("java", "synthetic-org/beta", (5, 777, 778), (1, 2, 2)),
    ("ruby", "synthetic-org/gamma", (42, 4200, 4201), (3, 2, 2)),
    ("python", "synthetic-org/delta", (31, 310), (3, 4)),
)
_STATUSES = ("passed", "passed", "passed", "failed", "errored", "canceled")


def _sha(*parts: object) -> str:
    return hashlib.sha1("/".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_rows() -> list[dict[str, str]]:
    """30 synthetic job rows (as raw CSV strings), in a shuffled, non-sorted order."""
    rng = random.Random(SEED)
    rows: list[dict[str, str]] = []
    job_id = 9_000
    for lang, project, build_ids, jobs in _PLAN:
        start = datetime(2014, 1, 1, 8, 0, 0, tzinfo=UTC)
        for i, (build_id, n_jobs) in enumerate(zip(build_ids, jobs, strict=True)):
            started = start + timedelta(days=7 * i, minutes=rng.randint(0, 600))
            is_pr = rng.random() < 0.4
            commits = [_sha(project, build_id, k) for k in range(rng.randint(1, 3))]
            status = _STATUSES[(build_id + i) % len(_STATUSES)]
            build = {
                "tr_build_id": str(build_id),
                "gh_project_name": project,
                "gh_is_pr": "TRUE" if is_pr else "FALSE",
                "gh_pr_created_at": _ts(started - timedelta(hours=5)) if is_pr else "NA",
                "gh_pull_req_num": str(rng.randint(1, 900)) if is_pr else "NA",
                "gh_lang": lang,
                "git_merged_with": "merge_button" if is_pr else "",
                "git_branch": "master" if not is_pr else f"feature-{build_id}",
                "gh_num_commits_in_push": "NA",
                "gh_commits_in_push": "NA",
                "git_prev_commit_resolution_status": "build_found" if i else "no_previous_build",
                "git_prev_built_commit": _sha(project, build_ids[i - 1], 0) if i else "",
                "tr_prev_build": str(build_ids[i - 1]) if i else "NA",
                "gh_first_commit_created_at": "NA",
                "gh_team_size": str(rng.randint(1, 30)),
                "git_all_built_commits": "#".join(commits),
                "git_num_all_built_commits": str(len(commits)),
                "git_trigger_commit": commits[0],
                "tr_virtual_merged_into": _sha("base", build_id) if is_pr else "",
                "gh_num_issue_comments": str(rng.randint(0, 5)) if is_pr else "NA",
                "gh_num_commit_comments": str(rng.randint(0, 2)),
                "gh_num_pr_comments": str(rng.randint(0, 9)) if is_pr else "NA",
                "git_diff_src_churn": str(rng.randint(0, 400)),
                "git_diff_test_churn": "0",
                "gh_diff_files_added": str(rng.randint(0, 4)),
                "gh_diff_files_deleted": str(rng.randint(0, 2)),
                "gh_diff_files_modified": str(rng.randint(0, 9)),
                "gh_diff_tests_added": str(rng.randint(0, 3)),
                "gh_diff_tests_deleted": str(rng.randint(0, 1)),
                "gh_diff_src_files": str(rng.randint(0, 6)),
                "gh_diff_doc_files": str(rng.randint(0, 1)),
                "gh_diff_other_files": str(rng.randint(0, 3)),
                "gh_num_commits_on_files_touched": str(rng.randint(0, 80)),
                "gh_sloc": str(rng.randint(1_000, 90_000)),
                "gh_test_lines_per_kloc": f"{rng.uniform(0, 900):.2f}",
                "gh_test_cases_per_kloc": f"{rng.uniform(0, 40):.4f}",
                "gh_asserts_cases_per_kloc": f"{rng.uniform(0, 90):.4f}",
                "gh_by_core_team_member": "TRUE" if rng.random() < 0.8 else "FALSE",
                "gh_description_complexity": str(rng.randint(3, 120)) if is_pr else "NA",
                "gh_pushed_at": "NA",
                "gh_build_started_at": _ts(started),
                "gh_repo_age": f"{400 + 7 * i + rng.random():.2f}",
                "gh_repo_num_commits": str(500 + 10 * i),
                "tr_build_number": str(1 + i),
                "tr_original_commit": commits[0],
                "tr_duration": str(rng.randint(60, 4_000)) if status != "canceled" else "NA",
                "tr_status": status,
            }
            job_ids = []
            for _ in range(n_jobs):
                job_id += rng.randint(1, 3)
                job_ids.append(job_id)
            build["tr_jobs"] = "#".join(map(str, job_ids))
            for j, jid in enumerate(job_ids):
                ran = rng.random() < 0.7
                failed_tests = ran and status == "failed"
                job = {
                    "tr_job_id": str(jid),
                    "tr_log_lan": lang,
                    "tr_log_status": "broken" if status in {"failed", "errored"} else "ok",
                    "tr_log_setup_time": str(rng.randint(0, 90)) if j % 2 == 0 else "NA",
                    "tr_log_analyzer": f"{lang}-analyzer",
                    "tr_log_frameworks": "junit" if ran else "",
                    "tr_log_bool_tests_ran": "TRUE" if ran else "FALSE",
                    "tr_log_bool_tests_failed": ("TRUE" if failed_tests else "FALSE")
                    if ran
                    else "",
                    "tr_log_num_tests_ok": str(rng.randint(0, 500)) if ran else "",
                    "tr_log_num_tests_failed": str(rng.randint(1, 4) if failed_tests else 0)
                    if ran
                    else "",
                    "tr_log_num_tests_run": str(rng.randint(0, 600)) if ran else "",
                    "tr_log_num_tests_skipped": str(rng.randint(0, 5)) if ran else "",
                    "tr_log_num_test_suites_run": "NA",
                    "tr_log_num_test_suites_ok": "NA",
                    "tr_log_num_test_suites_failed": "NA",
                    "tr_log_tests_failed": f"{FREE_TEXT_MARKER}#test, with comma"
                    if failed_tests
                    else "",
                    "tr_log_testduration": f"{rng.uniform(1, 300):.2f}" if ran else "NA",
                    "tr_log_buildduration": f"{rng.uniform(10, 900):.1f}" if ran else "NA",
                }
                rows.append({name: {**build, **job}[name] for name in EXPECTED_NAMES})
    rng.shuffle(rows)
    return rows


def render_csv(rows: list[dict[str, str]], header: list[str] | None = None) -> bytes:
    """CSV bytes (header + rows), minimal quoting, ``\\n`` line endings."""
    header = list(EXPECTED_NAMES) if header is None else header
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    return buf.getvalue().encode("utf-8")


def write_csv_gz(path: Path, rows: list[dict[str, str]], header: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(render_csv(rows, header))
    return path


# ---------------------------------------------------------------- in-memory S3 fake
def _client_error(code: str, status: int, op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


@dataclass
class FakeObject:
    body: bytes
    metadata: dict[str, str]
    content_type: str | None = None


@dataclass
class FakeS3Client:
    """Just enough of boto3's S3 client for :mod:`rl_pipeline.s3` (no network, no moto).

    Records every mutating call in :attr:`calls` (``(operation, bucket, key)``) so tests can
    assert ordering (e.g. manifest last) and idempotency (no writes on a re-run).
    """

    page_size: int = 1000
    buckets: dict[str, dict[str, FakeObject]] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _bucket(self, bucket: str, op: str) -> dict[str, FakeObject]:
        if bucket not in self.buckets:
            raise _client_error("NoSuchBucket", 404, op)
        return self.buckets[bucket]

    def head_bucket(self, Bucket: str) -> dict[str, Any]:
        if Bucket not in self.buckets:
            raise _client_error("404", 404, "HeadBucket")
        return {}

    def create_bucket(self, Bucket: str, **_: Any) -> dict[str, Any]:
        if Bucket in self.buckets:
            raise _client_error("BucketAlreadyOwnedByYou", 409, "CreateBucket")
        self.buckets[Bucket] = {}
        self.calls.append(("create_bucket", Bucket, ""))
        return {}

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        obj = self._bucket(Bucket, "HeadObject").get(Key)
        if obj is None:
            raise _client_error("404", 404, "HeadObject")
        return {"ContentLength": len(obj.body), "Metadata": dict(obj.metadata)}

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: bytes,
        Metadata: dict[str, str] | None = None,
        ContentType: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self._bucket(Bucket, "PutObject")[Key] = FakeObject(Body, dict(Metadata or {}), ContentType)
        self.calls.append(("put_object", Bucket, Key))
        return {}

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        ExtraArgs: dict[str, Any] | None = None,
    ) -> None:
        extra = ExtraArgs or {}
        self._bucket(Bucket, "PutObject")[Key] = FakeObject(
            Path(Filename).read_bytes(), dict(extra.get("Metadata") or {}), extra.get("ContentType")
        )
        self.calls.append(("upload_file", Bucket, Key))

    def list_objects_v2(
        self, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None, **_: Any
    ) -> dict[str, Any]:
        keys = sorted(k for k in self._bucket(Bucket, "ListObjectsV2") if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        out: dict[str, Any] = {
            "Contents": [{"Key": k, "Size": len(self.buckets[Bucket][k].body)} for k in page],
            "IsTruncated": truncated,
            "KeyCount": len(page),
        }
        if truncated:
            out["NextContinuationToken"] = str(start + self.page_size)
        return out

    def mutations(self) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] != "create_bucket"]


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(FIXTURE_NAME)
    out = write_csv_gz(target, build_rows())
    sys.stdout.write(f"wrote {out} ({out.stat().st_size} bytes)\n")
