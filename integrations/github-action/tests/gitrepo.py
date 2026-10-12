"""Throwaway git repositories with fixed authors and dates, driven through the git CLI."""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
from pathlib import Path

T0 = 1_767_225_600  # 2026-01-01T00:00:00Z
DAY = 86_400
ALICE = "Alice <alice@example.com>"
BOB = "Bob <bob@example.com>"


def iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repo:
    def __init__(self, path: Path, *, init: bool = True) -> None:
        self.path = path
        if init:
            path.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q", "-b", "main")

    def git(self, *args: str, when: int = T0, author: str = ALICE) -> str:
        m = re.fullmatch(r"(.*) <(.*)>", author)
        assert m, author
        name, email = m.group(1), m.group(2)
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_AUTHOR_DATE": f"{when} +0000",
                "GIT_COMMITTER_DATE": f"{when} +0000",
            }
        )
        proc = subprocess.run(
            ["git", *args], cwd=self.path, env=env, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip()

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def delete(self, rel: str) -> None:
        (self.path / rel).unlink()

    def commit(self, message: str, when: int, author: str = ALICE) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, when=when, author=author)
        return self.git("rev-parse", "HEAD")

    def checkout(self, *args: str) -> None:
        self.git("checkout", "-q", *args)

    def merge(self, branch: str, when: int, author: str = ALICE) -> str:
        self.git(
            "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch, when=when, author=author
        )
        return self.git("rev-parse", "HEAD")

    def clone_shallow(self, dest: Path, depth: int) -> Repo:
        self.git("clone", "-q", f"--depth={depth}", f"file://{self.path}", str(dest))
        return Repo(dest, init=False)
