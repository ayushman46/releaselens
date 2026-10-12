"""The composite action's shell steps: prepare.sh (inputs, context) and request.sh (HTTP)."""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from gitrepo import DAY, T0, Repo

import extract_change_facts as ecf

ACTION_DIR = Path(ecf.__file__).resolve().parent
KEY = "rlk_abcdefghijkl_S3CR3T-value-never-logged"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None, reason="needs bash and curl"
)


# --------------------------------------------------------------------------- request.sh


class _Api:
    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        self.replies = replies
        self.seen: list[dict[str, Any]] = []


@pytest.fixture
def api() -> Iterator[tuple[_Api, str]]:
    state = _Api([])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.seen.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            status, body = state.replies.pop(0) if state.replies else (500, None)
            payload = json.dumps(body).encode() if body is not None else b"upstream error"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _request(tmp_path: Path, url: str, **env: str) -> tuple[Path, subprocess.CompletedProcess[str]]:
    state = tmp_path / "state"
    state.mkdir()
    (state / "stage").write_text("ready")
    (state / "request.json").write_text(json.dumps({"change_facts": {"src_churn": 1}}))
    full_env = {
        **os.environ,
        "RL_STATE": str(state),
        "RL_API_URL": url,
        "RL_API_KEY": KEY,
        "RL_TIMEOUT": "20",
        "RL_RETRY_BASE_DELAY": "0",
        **env,
    }
    proc = subprocess.run(
        ["bash", str(ACTION_DIR / "scripts" / "request.sh")],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert KEY not in proc.stdout + proc.stderr
    assert not (state / "auth.header").exists()
    return state, proc


def test_request_retries_5xx_then_succeeds(tmp_path: Path, api: tuple[_Api, str]) -> None:
    server, url = api
    server.replies = [(502, None), (201, {"risk_score": 50, "risk_band": "MEDIUM"})]
    state, proc = _request(tmp_path, url)
    assert len(server.seen) == 2
    assert server.seen[0]["path"] == "/api/v1/risk/assessments"
    assert server.seen[0]["auth"] == f"Bearer {KEY}"
    assert server.seen[0]["body"] == {"change_facts": {"src_churn": 1}}
    assert (state / "http_status").read_text() == "201"
    assert (state / "stage").read_text() == "requested"
    assert json.loads((state / "response.json").read_text())["risk_band"] == "MEDIUM"
    assert "retrying" in proc.stdout


def test_request_does_not_retry_the_apis_own_503(tmp_path: Path, api: tuple[_Api, str]) -> None:
    server, url = api
    server.replies = [(503, {"error": {"code": "model_unavailable", "message": "no model"}})]
    state, _ = _request(tmp_path, url)
    assert len(server.seen) == 1
    assert (state / "http_status").read_text() == "503"


def test_request_does_not_retry_4xx(tmp_path: Path, api: tuple[_Api, str]) -> None:
    server, url = api
    server.replies = [(422, {"error": {"code": "validation_error", "message": "bad"}})]
    state, _ = _request(tmp_path, url)
    assert len(server.seen) == 1
    assert (state / "http_status").read_text() == "422"


def test_request_network_error_is_recorded(tmp_path: Path) -> None:
    state, proc = _request(tmp_path, "http://127.0.0.1:9", RL_MAX_ATTEMPTS="2")
    assert (state / "http_status").read_text() == "000"
    assert "network error after 2 attempt(s)" in (state / "message").read_text()
    assert "attempt 1 failed (HTTP 000)" in proc.stdout


# --------------------------------------------------------------------------- prepare.sh


def _prepare(repo: Path, tmp_path: Path, **env: str) -> tuple[dict[str, str], str, int]:
    gh_out = tmp_path / "gh_output"
    gh_out.write_text("")
    full_env = {**os.environ, "GITHUB_OUTPUT": str(gh_out), "RUNNER_TEMP": str(tmp_path), **env}
    proc = subprocess.run(
        ["bash", str(ACTION_DIR / "scripts" / "prepare.sh")],
        cwd=repo,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines() if "=" in line)
    return outputs, proc.stdout + proc.stderr, proc.returncode


def test_prepare_push_context_and_masking(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write("a.txt", "1\n")
    prev = r.commit("c1", T0)
    r.write("a.txt", "2\n")
    head = r.commit("c2", T0 + DAY)
    out, log, code = _prepare(
        r.path,
        tmp_path,
        RL_API_KEY=KEY,
        GH_EVENT_NAME="push",
        GH_SHA=head,
        GH_BEFORE=prev,
        GH_REF_NAME="main",
        RL_FAIL_ON_BAND="critical",
    )
    assert code == 0, log
    assert (out["event"], out["head"], out["previous"], out["branch"]) == (
        "push",
        head,
        prev,
        "main",
    )
    assert out["fail-on-band"] == "CRITICAL"
    assert out["api-url"] == "https://api.releaselens.dev"
    assert out["python"]
    # the only line carrying the key is the mask command the runner swallows
    assert [line for line in log.splitlines() if KEY in line] == [f"::add-mask::{KEY}"]
    assert (Path(out["state-dir"]) / "stage").read_text() == "ready"


def test_prepare_new_branch_and_missing_key(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write("a.txt", "1\n")
    head = r.commit("c1", T0)
    out, log, code = _prepare(
        r.path, tmp_path, RL_API_KEY="", GH_EVENT_NAME="push", GH_SHA=head, GH_BEFORE="0" * 40
    )
    assert code == 0, log
    assert out["previous"] == ""  # all-zero "before": no previous build
    state = Path(out["state-dir"])
    assert (state / "stage").read_text() == "skipped"
    assert "forks" in (state / "message").read_text()


def test_prepare_pr_uses_test_merge_commit_when_pr_head_is_missing(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write("a.txt", "1\n")
    base = r.commit("c1", T0)
    missing_pr_head = "ab" * 20  # recorded as a parent below, but not in the object store
    tree = r.git("rev-parse", "HEAD^{tree}")
    who = f"A <a@example.com> {T0} +0000"
    raw = (
        f"tree {tree}\nparent {base}\nparent {missing_pr_head}\n"
        f"author {who}\ncommitter {who}\n\ntest merge\n"
    )
    fake = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
        cwd=r.path,
        input=raw,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    r.git("update-ref", "HEAD", fake)
    out, log, code = _prepare(
        r.path,
        tmp_path,
        RL_API_KEY=KEY,
        GH_EVENT_NAME="pull_request",
        GH_SHA=fake,
        GH_PR_HEAD_SHA=missing_pr_head,
        GH_PR_BASE_SHA=base,
        GH_PR_NUMBER="42",
        GH_HEAD_REF="feature/x",
    )
    assert code == 0, log
    assert out["event"] == "pull_request"
    assert out["head"] == fake  # facts come from the checked-out test merge commit
    assert out["commit-sha"] == missing_pr_head  # but the PR head is what gets reported
    assert (out["previous"], out["pull-request"], out["branch"]) == (base, "42", "feature/x")


def test_prepare_rejects_bad_gate(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write("a.txt", "1\n")
    r.commit("c1", T0)
    _, log, code = _prepare(r.path, tmp_path, RL_FAIL_ON_BAND="MEDIUM")
    assert code == 1
    assert "fail-on-band must be none, HIGH or CRITICAL" in log


def test_action_yml_matches_scripts() -> None:
    text = (ACTION_DIR / "action.yml").read_text()
    assert f'RL_VERSION: "{ecf.EXTRACTOR_VERSION}"' in text
    declared = set(re.findall(r"^  ([a-z-]+):\n    description:", text, re.MULTILINE))
    used = set(re.findall(r"\$\{\{ inputs\.([a-z-]+) \}\}", text))
    assert used <= declared
    assert {"risk-score", "risk-band", "assessment-id", "details-url"} <= declared
