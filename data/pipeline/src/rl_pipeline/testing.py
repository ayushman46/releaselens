"""Test helpers: SYNTHETIC TravisTorrent-shaped fixture generators and an in-memory S3 fake.

**Nothing here is real data.** The fixtures use the real headers of the two registered releases
(66-column ``final-2017-01-25`` and 56-column ``travistorrent_5_3_2016``, from
:mod:`rl_pipeline.schema`); every value is made up by a seeded PRNG. Regenerate the committed
fixtures with::

    uv run python -m rl_pipeline.testing data/pipeline/tests/fixtures

The output is byte-stable (fixed seed, gzip mtime 0); unit tests guard against drift between
the generators and the committed files. :class:`FakeS3Client` implements just the boto3 S3 calls
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

from rl_pipeline.schema import FINAL_2017_NAMES, TT_5_3_2016_NAMES

FIXTURE_NAME = "travistorrent_synthetic_fixture.csv.gz"  # final-2017-01-25 layout
FIXTURE_2016_NAME = "travistorrent_5_3_2016_synthetic_fixture.csv.gz"
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
                rows.append({name: {**build, **job}[name] for name in FINAL_2017_NAMES})
    rng.shuffle(rows)
    return rows


# (lang, project, build ids, jobs per build) for the 5_3_2016 layout (java + ruby only, like the
# real release). One job of the first alpha build is also attached to the next build (a "foreign"
# row with the same tr_job_id, not listed in that build's tr_jobs), as measured in the release.
_PLAN_2016: tuple[tuple[str, str, tuple[int, ...], tuple[int, ...]], ...] = (
    ("java", "synthetic-org/alpha", (99, 100, 1000, 1001, 20000), (2, 1, 3, 2, 3)),
    ("java", "synthetic-org/beta", (5, 777, 778), (1, 2, 2)),
    ("ruby", "synthetic-org/gamma", (42, 4200, 4201, 4202), (3, 2, 2, 1)),
)
_STATUSES_2016 = ("passed", "passed", "failed", "errored", "passed", "canceled", "started")


def _london(dt: datetime) -> datetime:
    """Synthetic stand-in for Europe/London wall-clock (BST = UTC+1 from April to September)."""
    return dt + timedelta(hours=1) if 4 <= dt.month <= 9 else dt


def build_rows_2016() -> list[dict[str, str]]:
    """26 synthetic job rows in the 5_3_2016 layout; ``row`` numbers follow the file order."""
    rng = random.Random(SEED + 2016)
    rows: list[dict[str, str]] = []
    job_id = 7_000
    for lang, project, build_ids, jobs in _PLAN_2016:
        start = datetime(2014, 2, 20, 8, 0, 0, tzinfo=UTC)
        foreign: dict[str, str] | None = None
        for i, (build_id, n_jobs) in enumerate(zip(build_ids, jobs, strict=True)):
            started = start + timedelta(days=30 * i, minutes=rng.randint(0, 600))
            is_pr = rng.random() < 0.4
            commits = [_sha(project, build_id, k) for k in range(rng.randint(1, 3))]
            status = _STATUSES_2016[(build_id + i) % len(_STATUSES_2016)]
            # -1 placeholders, written as "-1.0" or "-1" like the release; ids sometimes "123.0".
            prev = (
                ("-1.0" if lang == "java" else "-1")
                if i == 0
                else (f"{build_ids[i - 1]}.0" if i % 2 else str(build_ids[i - 1]))
            )
            build = {
                "git_commit": _sha(project, build_id, "trigger"),
                "gh_project_name": project,
                "gh_is_pr": "true" if is_pr else "false",
                "git_merged_with": "merged_in_master" if is_pr else "",
                "gh_lang": lang,
                "git_branch": "master" if not is_pr else f"feature-{build_id}",
                "gh_first_commit_created_at": _ts(_london(started)),
                "gh_team_size": str(rng.randint(1, 30)),
                "git_commits": "#".join(commits),
                "git_num_commits": str(len(commits)),
                "gh_num_issue_comments": str(rng.randint(0, 5)),
                "gh_num_commit_comments": str(rng.randint(0, 2)),
                "gh_num_pr_comments": str(rng.randint(0, 9)),
                "gh_src_churn": str(rng.randint(0, 400)),
                "gh_test_churn": str(rng.choice((0, 0, rng.randint(1, 90)))),
                "gh_files_added": str(rng.randint(0, 4)),
                "gh_files_deleted": str(rng.randint(0, 2)),
                "gh_files_modified": str(rng.randint(1, 9)),
                "gh_tests_added": str(rng.randint(0, 3)),
                "gh_tests_deleted": str(rng.randint(0, 1)),
                "gh_src_files": str(rng.randint(0, 3)),
                "gh_doc_files": str(rng.randint(0, 1)),
                "gh_other_files": str(rng.randint(0, 2)),
                "gh_commits_on_files_touched": str(rng.randint(0, 80)),
                "gh_sloc": str(rng.randint(1_000, 90_000)),
                "gh_test_lines_per_kloc": f"{rng.uniform(0, 900):.6f}",
                "gh_test_cases_per_kloc": f"{rng.uniform(0, 40):.6f}",
                "gh_asserts_cases_per_kloc": f"{rng.uniform(0, 90):.6f}",
                "gh_by_core_team_member": "true" if rng.random() < 0.8 else "false",
                "gh_description_complexity": str(rng.randint(3, 120)) if is_pr else "NA",
                "tr_build_id": str(build_id),
                "gh_pull_req_num": str(rng.randint(1, 900)) if is_pr else "NA",
                "tr_status": status,
                "tr_duration": str(rng.randint(60, 4_000)) if status != "started" else "NA",
                "tr_started_at": _ts(started),
                "tr_build_number": str(1 + i),
                "git_num_committers": str(rng.randint(1, len(commits) + 1)),
                "tr_prev_build": prev,
                "tr_ci_latency": "-1" if i % 3 == 0 else str(rng.randint(-5000, 900)),
            }
            job_ids = []
            for _ in range(n_jobs):
                job_id += rng.randint(1, 3)
                job_ids.append(job_id)
            build["tr_jobs"] = "[" + ", ".join(map(str, job_ids)) + "]"
            build["tr_num_jobs"] = str(n_jobs)
            for j, jid in enumerate(job_ids):
                ran = rng.random() < 0.7
                failed_tests = ran and status == "failed"
                job = {
                    "tr_job_id": str(jid),
                    "tr_lan": lang,
                    "tr_setup_time": str(rng.randint(0, 90)) if j % 2 == 0 else "NA",
                    "tr_analyzer": "java-maven" if lang == "java" else "ruby",
                    "tr_frameworks": ("junit" if lang == "java" else "rspec") if ran else "",
                    "tr_tests_ok": str(rng.randint(0, 500)) if ran else "NA",
                    "tr_tests_fail": str(rng.randint(1, 4) if failed_tests else 0) if ran else "NA",
                    "tr_tests_run": str(rng.randint(0, 600)) if ran else "NA",
                    "tr_tests_skipped": str(rng.randint(0, 5)) if ran else "NA",
                    "tr_failed_tests": f'{FREE_TEXT_MARKER}#test, with "quotes"'
                    if failed_tests
                    else "",
                    "tr_testduration": f"{rng.uniform(1, 300):.2f}" if ran else "NA",
                    "tr_purebuildduration": f"{rng.randint(10, 900)}.0" if ran else "",
                    "tr_tests_ran": "true" if ran else "false",
                    "tr_tests_failed": "true" if failed_tests else "false",
                }
                row = {**build, **job}
                rows.append(row)
                if foreign is None and lang == "java" and i == 0:
                    foreign = row
            if foreign is not None and i == 1:
                # the same job row attached to this (wrong) build: build columns from here
                rows.append({**foreign, **build, "tr_job_id": foreign["tr_job_id"]})
    rng.shuffle(rows)
    for n, row in enumerate(rows, start=1):
        row["row"] = str(n)
    return [{name: row[name] for name in TT_5_3_2016_NAMES} for row in rows]


def render_csv(rows: list[dict[str, str]], header: list[str] | None = None) -> bytes:
    """CSV bytes (header + rows), minimal quoting, ``\\n`` line endings."""
    header = list(FINAL_2017_NAMES) if header is None else header
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


def write_fixtures(directory: Path) -> list[Path]:
    """(Re)write both committed synthetic fixtures into ``directory``."""
    return [
        write_csv_gz(directory / FIXTURE_NAME, build_rows()),
        write_csv_gz(directory / FIXTURE_2016_NAME, build_rows_2016(), list(TT_5_3_2016_NAMES)),
    ]


if __name__ == "__main__":
    import sys

    for out in write_fixtures(Path(sys.argv[1]) if len(sys.argv) > 1 else Path()):
        sys.stdout.write(f"wrote {out} ({out.stat().st_size} bytes)\n")
