#!/usr/bin/env python3
"""ReleaseLens change-fact extractor.

Computes the aggregate change facts defined in ``docs/data/FEATURE_DEFINITIONS.md`` (§1 built
commits, §2 facts) from a local git checkout and prints the request body for
``POST /api/v1/risk/assessments`` (``docs/api/RISK_ASSESSMENTS.md``).

* Python 3.9+ and the standard library only, so it runs on any GitHub runner without
  ``pip install``. All repository access goes through the ``git`` CLI.
* Deterministic: the same repository state and arguments give byte-identical output.
* Unknown or impossible values are ``null``, never ``0``. Why a fact is ``null`` is written to
  stderr and, with ``--diagnostics-out``, to a JSON file.
* Privacy: only integer counts, ratios and booleans are emitted. No source code, file contents,
  file paths or commit messages appear in the output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

EXTRACTOR_NAME = "releaselens-action"
EXTRACTOR_VERSION = "0.1.0"

WINDOW_DAYS = 90  # "3 months before the build start" (commits_on_files_touched, team_size)
SECONDS_PER_DAY = 86_400

# Work caps. Exceeding one makes the affected facts null (never a partial, wrong count).
DEFAULT_MAX_BUILT_COMMITS = 1_000  # per-commit diffs beyond this are not computed
DEFAULT_MAX_SLOC_BYTES = 256 * 1024 * 1024  # total size of the language's files at head
DEFAULT_MAX_SLOC_FILES = 100_000  # number of the language's files at head
RENAME_LIMIT = 1_000  # git -l<n>; pinned so user config cannot change rename detection
PATHSPEC_CHUNK = 200  # paths per `git log -- <paths>` call (command-line length)
MAX_PATHSPEC_FOR_TEST_DIFF = 2_000  # above this, the test-case diff runs unrestricted
COUNTER_VERSION = 1  # bump when a line counter changes (invalidates --cache-file entries)

_HEX_SHA = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_ZERO_SHA = re.compile(r"\A0+\Z")

# --------------------------------------------------------------------------- file-type table
# Pinned v0.1 table derived from GitHub Linguist's languages.yml ``type`` field
# (programming / markup / prose / data). Lower-case extensions. Filenames are checked first.

PROGRAMMING_FILENAMES = frozenset(
    {
        "BUILD",
        "BUILD.bazel",
        "CMakeLists.txt",
        "Containerfile",
        "Dockerfile",
        "Gemfile",
        "GNUmakefile",
        "Jenkinsfile",
        "Makefile",
        "Rakefile",
        "Vagrantfile",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "makefile",
    }
)
PROGRAMMING_EXTENSIONS = frozenset(
    {
        ".aj", ".asm", ".awk", ".bash", ".bat", ".bzl", ".c", ".c++", ".cc", ".cjs", ".clj",
        ".cljc", ".cljs", ".cmake", ".cmd", ".coffee", ".cpp", ".cs", ".cts", ".cu", ".cuh",
        ".cxx", ".d", ".dart", ".dockerfile", ".el", ".elm", ".erl", ".ex", ".exs", ".f",
        ".f03", ".f90", ".f95", ".fish", ".for", ".fs", ".fsi", ".fsx", ".gemspec", ".go",
        ".groovy", ".gvy", ".gy", ".h", ".hcl", ".hh", ".hpp", ".hrl", ".hs", ".hxx", ".inl",
        ".java", ".jl", ".js", ".jsonnet", ".jsp", ".jsx", ".kt", ".kts", ".ksh", ".lhs",
        ".libsonnet", ".lisp", ".lua", ".m", ".mjs", ".ml", ".mli", ".mm", ".mts", ".nim",
        ".nix", ".pas", ".php", ".pl", ".pm", ".ps1", ".psm1", ".py", ".pyi", ".pyw", ".pyx",
        ".r", ".rake", ".rb", ".rkt", ".rs", ".s", ".sc", ".scala", ".scm", ".sh", ".sol",
        ".swift", ".tcl", ".tf", ".ts", ".tsx", ".vala", ".vb", ".xtend", ".zig", ".zsh",
    }
)  # fmt: skip
DOC_FILENAMES = frozenset(
    {
        "AUTHORS",
        "CHANGELOG",
        "CHANGES",
        "CONTRIBUTORS",
        "COPYING",
        "COPYRIGHT",
        "HISTORY",
        "INSTALL",
        "LICENCE",
        "LICENSE",
        "NEWS",
        "NOTICE",
        "README",
        "TODO",
    }
)
DOC_EXTENSIONS = frozenset(
    {
        # prose
        ".adoc", ".asciidoc", ".bib", ".creole", ".ltx", ".markdown", ".md", ".mdown", ".mdx",
        ".mediawiki", ".mkd", ".mkdn", ".org", ".pod", ".rdoc", ".rest", ".rst", ".tex",
        ".text", ".textile", ".txt", ".wiki",
        # markup
        ".css", ".ejs", ".erb", ".haml", ".handlebars", ".hbs", ".htm", ".html", ".jade",
        ".less", ".mustache", ".pug", ".sass", ".scss", ".slim", ".styl", ".svelte", ".twig",
        ".vue", ".xhtml",
    }
)  # fmt: skip


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def is_programming_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in PROGRAMMING_FILENAMES or _suffix(path) in PROGRAMMING_EXTENSIONS


def is_doc_file(path: str) -> bool:
    p = PurePosixPath(path)
    if p.name in PROGRAMMING_FILENAMES:
        return False
    if not p.suffix and p.name.upper() in DOC_FILENAMES:
        return True
    return _suffix(path) in DOC_EXTENSIONS


# --------------------------------------------------------------------------- languages


@dataclass(frozen=True)
class BlobCounts:
    code_lines: int  # non-blank lines after removing comments
    test_case_lines: int  # code lines matching the test-case pattern
    assert_lines: int  # code lines matching the assert pattern


@dataclass(frozen=True)
class LanguageSpec:
    """Everything language-specific. Add a language by adding one entry to ``LANGUAGES``."""

    name: str
    extensions: frozenset[str]
    test_dir_names: frozenset[str]  # a path component equal to one of these → test file
    test_filename_globs: tuple[str, ...]  # fnmatch (case-sensitive) on the file name
    # Lexer: each match is a comment (named group ``c``) or a string/char literal (anything
    # else). Matches are found left to right, so ``//`` inside a string is never a comment.
    lexer: re.Pattern[str]
    # MULTILINE patterns, each matching one whole (comment-stripped) line.
    test_case_line: re.Pattern[str]
    assert_line: re.Pattern[str]

    def has_extension(self, path: str) -> bool:
        return _suffix(path) in self.extensions

    def is_test_path(self, path: str) -> bool:
        p = PurePosixPath(path)
        if any(part in self.test_dir_names for part in p.parts[:-1]):
            return True
        return any(fnmatch.fnmatchcase(p.name, g) for g in self.test_filename_globs)

    def strip(self, text: str) -> str:
        """Remove comments and blank out literals, preserving the line structure."""
        return self.lexer.sub(_lexer_replacement, text)

    def count(self, data: bytes) -> BlobCounts:
        if b"\0" in data[:8000]:  # binary
            return BlobCounts(0, 0, 0)
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        code = self.strip(text)
        return BlobCounts(
            code_lines=len(_NONBLANK_LINE.findall(code)),
            test_case_lines=len(self.test_case_line.findall(code)),
            assert_lines=len(self.assert_line.findall(code)),
        )

    def is_test_case_line(self, line: str) -> bool:
        """For one diff line (no block-comment context): skip comment lines, strip the rest."""
        stripped = line.strip()
        if stripped.startswith(("//", "/*", "*")):
            return False
        return self.test_case_line.search(self.strip(line)) is not None


_NONBLANK_LINE = re.compile(r"^[^\S\n]*\S", re.MULTILINE)


def _lexer_replacement(m: re.Match[str]) -> str:
    tok = m.group(0)
    if m.group("c") is not None:  # comment: keep only its line breaks (or one separating space)
        n = tok.count("\n")
        return "\n" * n if n else " "
    # string / char / text block: an empty literal on every non-blank line it spans
    return "\n".join('""' if seg.strip() else "" for seg in tok.split("\n"))


JAVA = LanguageSpec(
    name="java",
    extensions=frozenset({".java"}),
    test_dir_names=frozenset({"test", "tests"}),  # includes src/test/
    test_filename_globs=("*Test.java", "*Tests.java", "Test*.java"),
    lexer=re.compile(
        r'"""(?:\\[\s\S]|[^\\])*?(?:"""|\Z)'  # text block (Java 15+)
        r'|"(?:[^"\\\n]|\\.)*"?'  # string literal (an unterminated one ends at the line end)
        r"|'(?:[^'\\\n]|\\.)*'?"  # char literal
        r"|(?P<c>//[^\n]*|/\*[\s\S]*?(?:\*/|\Z))"  # line comment, block comment / Javadoc
    ),
    test_case_line=re.compile(
        r"^[^\n]*?(?:@Test\b|\bvoid[ \t]+test\w*[ \t]*\()[^\n]*", re.MULTILINE
    ),
    assert_line=re.compile(
        r"^(?:[ \t]*assert\b|[^\n]*?\bassert(?:[A-Z_]\w*)?[ \t]*\()[^\n]*", re.MULTILINE
    ),
)

LANGUAGES: dict[str, LanguageSpec] = {JAVA.name: JAVA}


# --------------------------------------------------------------------------- classification


def split_globs(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = re.split(r"[,\n]", value)
    return tuple(p.strip() for p in parts if p.strip())


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` crosses directories, ``*``/``?`` do not. No ``/`` in the pattern → match the name."""
    p = pattern.lstrip("/")
    out: list[str] = []
    i = 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if p.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = p.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = p[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


class Classifier:
    """src / test / doc / other for any path, plus the language filter for sloc and test cases.

    Defaults follow FEATURE_DEFINITIONS §2. ``src_globs`` / ``test_globs`` overrides replace the
    extension table (programming files) and the language's test rules respectively.
    """

    def __init__(
        self, lang: LanguageSpec, src_globs: Sequence[str] = (), test_globs: Sequence[str] = ()
    ) -> None:
        self.lang = lang
        self._src = [(g, glob_to_regex(g)) for g in src_globs]
        self._test = [(g, glob_to_regex(g)) for g in test_globs]

    @staticmethod
    def _matches(globs: list[tuple[str, re.Pattern[str]]], path: str) -> bool:
        name = PurePosixPath(path).name
        return any(rx.match(path if "/" in g.strip("/") else name) for g, rx in globs)

    def is_code(self, path: str) -> bool:
        if self._test and self._matches(self._test, path):
            return True
        if self._src:
            return self._matches(self._src, path)
        return is_programming_file(path)

    def is_test(self, path: str) -> bool:
        if not self.is_code(path):
            return False
        if self._test:
            return self._matches(self._test, path)
        return self.lang.is_test_path(path)

    def kind(self, path: str) -> str:
        if self.is_code(path):
            return "test" if self.is_test(path) else "src"
        return "doc" if is_doc_file(path) else "other"

    def is_language_file(self, path: str) -> bool:
        return self.lang.has_extension(path) and self.is_code(path)


# --------------------------------------------------------------------------- git access


class GitError(RuntimeError):
    pass


class Git:
    """Thin ``git`` CLI wrapper. Config that could change parsed output is pinned per call."""

    def __init__(self, repo: Path) -> None:
        exe = shutil.which("git")
        if exe is None:
            raise GitError("git executable not found on PATH")
        self.exe = exe
        self.repo = repo
        self.env = dict(os.environ)
        self.env.update(
            {
                "LC_ALL": "C",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
            }
        )

    def _argv(self, args: Sequence[str]) -> list[str]:
        pinned = (
            "-c", "log.showSignature=false",
            "-c", "color.ui=false",
            "-c", "core.quotePath=false",
            "-c", "i18n.logOutputEncoding=UTF-8",
        )  # fmt: skip
        return [self.exe, "-C", str(self.repo), *pinned, *args]

    def run(self, *args: str, stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(  # noqa: S603 - fixed git argv, never a shell
            self._argv(args),
            input=stdin,
            capture_output=True,
            env=self.env,
            check=False,
        )
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {args[0]} failed ({proc.returncode}): {msg}")
        return proc.stdout

    def ok(self, *args: str) -> bool:
        try:
            self.run(*args)
        except GitError:
            return False
        return True

    def stream_lines(self, *args: str) -> Iterator[bytes]:
        """Yield stdout lines; the process is killed if the caller stops early."""
        proc = subprocess.Popen(  # noqa: S603 - fixed git argv, never a shell
            self._argv(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        done = False
        try:
            for line in proc.stdout:
                yield line.rstrip(b"\n")
            done = True
        finally:
            if not done:
                proc.kill()
            proc.stdout.close()
            err = proc.stderr.read()
            proc.stderr.close()
            rc = proc.wait()
        if rc != 0:
            msg = err.decode("utf-8", "replace").strip()
            raise GitError(f"git {args[0]} failed ({rc}): {msg}")

    def resolve_commit(self, rev: str) -> str | None:
        try:
            out = self.run(
                "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"
            )  # fmt: skip
        except GitError:
            return None
        sha = out.decode().strip()
        return sha or None

    def parents(self, sha: str) -> list[str]:
        out = self.run("rev-list", "--parents", "-n", "1", sha).decode().split()
        return out[1:]

    def committer_time(self, sha: str) -> int:
        return int(self.run("log", "-1", "--format=%ct", sha).decode().strip())

    def empty_tree(self) -> str:
        return self.run("hash-object", "-t", "tree", "--stdin", stdin=b"").decode().strip()

    def shallow_commits(self) -> frozenset[str] | None:
        """Shallow-boundary commits, or None when the repository is a full clone."""
        flag = self.run("rev-parse", "--is-shallow-repository").decode().strip()
        if flag != "true":
            return frozenset()
        rel = self.run("rev-parse", "--git-path", "shallow").decode().strip()
        path = Path(rel) if Path(rel).is_absolute() else self.repo / rel
        try:
            lines = path.read_text(encoding="ascii").split()
        except OSError:
            return None
        return frozenset(lines)


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


# --------------------------------------------------------------------------- built commits (§1)


@dataclass
class Walk:
    commits: list[str] | None  # newest first; None = the built set is unknown
    parents: dict[str, list[str]]
    stop: str  # build_found | merge_found | root | no_previous_build | previous_missing | shallow
    count_known: bool  # num_all_built_commits is exact
    diffable: bool  # every built commit's first parent is available


def walk_built_commits(
    git: Git, head: str, previous: str | None, previous_given: bool, shallow: frozenset[str]
) -> Walk:
    """FEATURE_DEFINITIONS §1: walk from head; stop at the previous built commit (excluded), at a
    merge commit (included) or at the root. No previous build → only the head commit."""
    if previous is None:
        if previous_given:  # a previous build exists but its commit is not in this clone
            return Walk(None, {}, "previous_missing", count_known=False, diffable=False)
        return Walk(
            [head], {head: git.parents(head)}, "no_previous_build", True, head not in shallow
        )

    commits: list[str] = []
    parents: dict[str, list[str]] = {}
    stop = "build_found"
    for line in git.stream_lines("rev-list", "--first-parent", "--parents", head, f"^{previous}"):
        shas = line.decode().split()
        if not shas:
            continue
        sha, ps = shas[0], shas[1:]
        commits.append(sha)
        parents[sha] = ps
        if sha in shallow:  # parents cut off by a shallow fetch: the walk cannot continue
            stop = "shallow"
            break
        if len(ps) > 1:
            stop = "merge_found"
            break
        if not ps:
            stop = "root"
            break
    if not commits:
        # head is the previous built commit or one of its ancestors (re-run, revert push): the
        # trigger commit is always part of its own build.
        return Walk([head], {head: git.parents(head)}, "build_found", True, head not in shallow)
    ok = stop != "shallow"
    return Walk(commits, parents, stop, count_known=ok, diffable=ok)


# --------------------------------------------------------------------------- per-commit changes


@dataclass
class ChangeSummary:
    src_churn: int = 0
    test_churn: int = 0
    added: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    modified: set[str] = field(default_factory=set)
    touched: set[str] = field(default_factory=set)  # A/D/M/T paths + rename destinations
    history_paths: set[str] = field(default_factory=set)  # touched + rename sources


_DIFF_OPTS = (
    "-r",
    "-M",
    f"-l{RENAME_LIMIT}",
    "--diff-algorithm=myers",
    "--no-ext-diff",
    "--no-textconv",
)


@dataclass(frozen=True)
class RawEntry:
    status: str  # A D M T R C U X
    src: str
    dst: str


def _parse_raw_and_numstat(
    out: bytes,
) -> tuple[list[RawEntry], list[tuple[int | None, str]]]:
    """Parse ``diff-tree -z --raw --numstat`` output (optionally with commit-id header tokens).

    Returns raw entries and numstat rows ``(added + deleted or None for binary, path)``.
    """
    toks = out.split(b"\0")
    raw: list[RawEntry] = []
    num: list[tuple[int | None, str]] = []
    i = 0
    n = len(toks)
    while i < n:
        tok = toks[i]
        if not tok:
            i += 1
        elif tok.startswith(b":"):
            status = tok.split(b" ")[-1].decode()[:1]
            if status in ("R", "C"):
                src, dst = _decode_path(toks[i + 1]), _decode_path(toks[i + 2])
                i += 3
            else:
                src = dst = _decode_path(toks[i + 1])
                i += 2
            raw.append(RawEntry(status, src, dst))
        elif b"\t" in tok:
            a, d, rest = tok.split(b"\t", 2)
            if rest:
                path = _decode_path(rest)
                i += 1
            else:  # rename/copy: the two paths follow as separate tokens
                path = _decode_path(toks[i + 2])
                i += 3
            lines = None if a == b"-" or d == b"-" else int(a) + int(d)
            num.append((lines, path))
        else:  # commit-id header from --stdin
            i += 1
    return raw, num


def summarize_commits(
    git: Git, commits: Sequence[str], parents: dict[str, list[str]], cls: Classifier
) -> ChangeSummary:
    """Churn and file statuses summed over the built commits; a merge is diffed against its
    first parent, a root commit against the empty tree."""
    lines = []
    for c in commits:
        ps = parents[c]
        lines.append(f"{c} {ps[0]}" if ps else c)
    out = git.run(
        "diff-tree", "--stdin", "--root", "-z", "--raw", "--numstat", *_DIFF_OPTS,
        stdin=("\n".join(lines) + "\n").encode(),
    )  # fmt: skip
    raw, num = _parse_raw_and_numstat(out)
    s = ChangeSummary()
    for e in raw:
        if e.status == "A":
            s.added.add(e.dst)
        elif e.status == "D":
            s.deleted.add(e.dst)
        elif e.status == "M":
            s.modified.add(e.dst)
        # R (rename), C, T (type change), U, X: in none of added/deleted/modified
        s.touched.add(e.dst)
        s.history_paths.update((e.src, e.dst))
    for lines_changed, path in num:
        if lines_changed is None:  # binary: numstat "-"
            continue
        kind = cls.kind(path)
        if kind == "src":
            s.src_churn += lines_changed
        elif kind == "test":
            s.test_churn += lines_changed
    return s


# --------------------------------------------------------------------------- tests added/deleted

_HUNK = re.compile(rb"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _patch_lines(patch: bytes) -> Iterator[tuple[int, str, bytes]]:
    """``(file index, '+'|'-', content)`` for every changed line of a ``-U0`` patch.

    Hunk bodies are consumed by their header counts, so a removed line whose content starts
    with ``--`` (shown as ``---``) is never mistaken for a file header.
    """
    file_idx = -1
    rem_old = rem_new = 0
    for line in patch.split(b"\n"):
        if rem_old or rem_new:
            if rem_old and line.startswith(b"-"):
                rem_old -= 1
                yield file_idx, "-", line[1:]
                continue
            if rem_new and line.startswith(b"+"):
                rem_new -= 1
                yield file_idx, "+", line[1:]
                continue
            if line.startswith(b"\\"):  # "\ No newline at end of file"
                continue
            rem_old = rem_new = 0
        if line.startswith(b"diff --git "):
            file_idx += 1
        elif line.startswith(b"@@ "):
            m = _HUNK.match(line)
            if m:
                rem_old = int(m.group(1)) if m.group(1) is not None else 1
                rem_new = int(m.group(2)) if m.group(2) is not None else 1


def count_test_case_changes(
    git: Git, base: str, head: str, cls: Classifier
) -> tuple[int, int] | None:
    """Test cases added / deleted in the language's test files, diffing base against head."""

    def raw_entries(paths: Sequence[str]) -> list[RawEntry]:
        out = git.run("diff-tree", "-z", "--raw", *_DIFF_OPTS, base, head, "--", *paths)
        return _parse_raw_and_numstat(out)[0]

    def entry_path(e: RawEntry) -> str:
        return e.dst  # for deletions src == dst

    candidates = [e for e in raw_entries(()) if cls.is_language_file(entry_path(e))]
    candidates = [e for e in candidates if cls.is_test(entry_path(e))]
    if not candidates:
        return 0, 0
    pathspec = sorted({p for e in candidates for p in (e.src, e.dst)})
    if len(pathspec) > MAX_PATHSPEC_FOR_TEST_DIFF:
        pathspec = []
    entries = raw_entries(pathspec)
    patch = git.run(
        "diff-tree", "-p", "-U0", "--no-commit-id", *_DIFF_OPTS, base, head, "--", *pathspec
    )
    lang = cls.lang
    added = deleted = 0
    for idx, sign, content in _patch_lines(patch):
        if idx < 0 or idx >= len(entries):
            return None
        path = entry_path(entries[idx])
        if not (cls.is_language_file(path) and cls.is_test(path)):
            continue
        if lang.is_test_case_line(content.decode("utf-8", "replace")):
            if sign == "+":
                added += 1
            else:
                deleted += 1
    blocks = sum(1 for line in patch.split(b"\n") if line.startswith(b"diff --git "))
    if blocks != len(entries):  # patch and raw listings disagree: do not guess
        return None
    return added, deleted


# --------------------------------------------------------------------------- sloc / test density


@dataclass(frozen=True)
class CodebaseCounts:
    sloc: int
    test_lines: int
    test_cases: int
    asserts: int


def _cache_key(lang: LanguageSpec) -> str:
    return f"{lang.name}:{COUNTER_VERSION}"


def load_cache(path: Path | None, lang: LanguageSpec) -> dict[str, BlobCounts]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("counter") != _cache_key(lang):
        return {}
    blobs = data.get("blobs")
    if not isinstance(blobs, dict):
        return {}
    out: dict[str, BlobCounts] = {}
    for sha, v in blobs.items():
        if isinstance(v, list) and len(v) == 3 and all(isinstance(x, int) for x in v):
            out[sha] = BlobCounts(v[0], v[1], v[2])
    return out


def save_cache(path: Path, lang: LanguageSpec, blobs: dict[str, BlobCounts]) -> None:
    payload = {
        "counter": _cache_key(lang),
        "blobs": {
            k: [v.code_lines, v.test_case_lines, v.assert_lines] for k, v in sorted(blobs.items())
        },
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _read_blobs(git: Git, shas: Sequence[str]) -> Iterator[tuple[str, bytes]]:
    proc = subprocess.Popen(  # noqa: S603 - fixed git argv, never a shell
        git._argv(("cat-file", "--batch")),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=git.env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        for sha in shas:
            proc.stdin.write(sha.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) < 3 or header[1] != b"blob":
                raise GitError(f"cat-file: object {sha} is not an available blob")
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing LF
            yield sha, data
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.kill()
        proc.wait()


def count_codebase(
    git: Git,
    head: str,
    cls: Classifier,
    max_bytes: int,
    max_files: int,
    cache: dict[str, BlobCounts],
) -> tuple[CodebaseCounts | None, str | None, set[str]]:
    """Comment- and blank-stripped line counts of the language's files tracked at head.

    Returns ``(counts, None, blob ids used)``, or ``(None, reason, set())`` when a work cap is
    exceeded. The caps are checked on the tree before anything is read, so the result never
    depends on the cache state.
    """
    out = git.run("ls-tree", "-r", "-z", "-l", "--full-tree", head)
    files: list[tuple[str, bool]] = []  # (blob sha, is_test)
    total = 0
    for tok in out.split(b"\0"):
        if not tok:
            continue
        meta, raw_path = tok.split(b"\t", 1)
        mode, typ, raw_sha, size = meta.split()
        if typ != b"blob" or mode == b"120000":  # skip submodules and symlinks
            continue
        path = _decode_path(raw_path)
        if not cls.is_language_file(path):
            continue
        files.append((raw_sha.decode(), cls.is_test(path)))
        total += int(size) if size != b"-" else 0
    if len(files) > max_files:
        reason = f"{len(files)} {cls.lang.name} files at head exceed the cap of {max_files}"
        return None, reason, set()
    if total > max_bytes:
        reason = f"{total} bytes of {cls.lang.name} files exceed the cap of {max_bytes}"
        return None, reason, set()

    needed = sorted({blob for blob, _ in files if blob not in cache})
    for blob, data in _read_blobs(git, needed):
        cache[blob] = cls.lang.count(data)

    sloc = test_lines = test_cases = asserts = 0
    for blob, is_test in files:
        c = cache[blob]
        if is_test:
            test_lines += c.code_lines
            test_cases += c.test_case_lines
            asserts += c.assert_lines
        else:
            sloc += c.code_lines
    used = {blob for blob, _ in files}
    return CodebaseCounts(sloc, test_lines, test_cases, asserts), None, used


def per_kloc(value: int, sloc: int) -> float | None:
    if sloc <= 0:
        return None  # undefined, not zero
    return round(value * 1000.0 / sloc, 4)


# --------------------------------------------------------------------------- history facts


def history_window_complete(
    git: Git, head: str, shallow: frozenset[str], window_start: int
) -> bool:
    """True when every commit reachable from head with a committer date inside the window is
    present. In a shallow clone that holds when all reachable boundary commits predate it."""
    if not shallow:
        return True
    for line in git.stream_lines("log", "--format=%H %ct", head):
        sha, _, ct = line.decode().partition(" ")
        if sha in shallow and int(ct) >= window_start:
            return False
    return True


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def commits_on_files_touched(
    git: Git, head: str, paths: Iterable[str], window_start: int, start: int, built: set[str]
) -> int:
    """Distinct non-merge commits reachable from head, committed in [start-90d, start], that
    modified any touched path; the built commits themselves are excluded."""
    found: set[str] = set()
    for chunk in _chunks(sorted(paths), PATHSPEC_CHUNK):
        out = git.run(
            "log", "--full-history", "--no-merges", f"--max-age={window_start}",
            "--format=%H %ct", head, "--", *chunk,
        )  # fmt: skip
        for line in out.decode().splitlines():
            sha, _, ct = line.partition(" ")
            if window_start <= int(ct) <= start and sha not in built:
                found.add(sha)
    return len(found)


def team_emails(git: Git, head: str, window_start: int, start: int, built: set[str]) -> set[str]:
    """Mailmap-normalised author emails of commits (merges included: mergers count) reachable
    from head and committed in [start-90d, start], excluding the built commits."""
    out = git.run("log", f"--max-age={window_start}", "--format=%H%x1f%ct%x1f%aE", head)
    team: set[str] = set()
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, ct, email = parts
        email = email.strip().lower()
        if email and window_start <= int(ct) <= start and sha not in built:
            team.add(email)
    return team


def author_emails(git: Git, commits: Sequence[str]) -> set[str]:
    out = git.run(
        "log", "--no-walk=unsorted", "--stdin", "--format=%aE",
        stdin=("\n".join(commits) + "\n").encode(),
    )  # fmt: skip
    return {e.strip().lower() for e in out.decode("utf-8", "replace").splitlines()}


def repo_age_days(git: Git, head: str, start: int) -> int | None:
    out = git.run("rev-list", "--max-parents=0", "--timestamp", head).decode().split("\n")
    times = [int(line.split()[0]) for line in out if line.strip()]
    if not times:
        return None
    age = (start - min(times)) // SECONDS_PER_DAY
    return age if age >= 0 else None


# --------------------------------------------------------------------------- orchestration

FACT_ORDER = (
    "src_churn",
    "test_churn",
    "files_added",
    "files_deleted",
    "files_modified",
    "tests_added",
    "tests_deleted",
    "src_files",
    "doc_files",
    "other_files",
    "sloc",
    "test_lines_per_kloc",
    "test_cases_per_kloc",
    "asserts_per_kloc",
    "num_all_built_commits",
    "team_size",
    "by_core_team_member",
    "is_pr",
    "commits_on_files_touched",
    "repo_age_days",
)
DIFF_FACTS = (
    "src_churn",
    "test_churn",
    "files_added",
    "files_deleted",
    "files_modified",
    "tests_added",
    "tests_deleted",
    "src_files",
    "doc_files",
    "other_files",
)
SLOC_FACTS = ("sloc", "test_lines_per_kloc", "test_cases_per_kloc", "asserts_per_kloc")


@dataclass
class Options:
    repo: Path
    head: str
    previous: str | None = None
    language: str = "java"
    event: str = "push"
    triggered_at: str | None = None
    repository: str | None = None
    pull_request: int | None = None
    branch: str | None = None
    ci_run_url: str | None = None
    commit_sha: str | None = None
    src_globs: tuple[str, ...] = ()
    test_globs: tuple[str, ...] = ()
    max_built_commits: int = DEFAULT_MAX_BUILT_COMMITS
    max_sloc_bytes: int = DEFAULT_MAX_SLOC_BYTES
    max_sloc_files: int = DEFAULT_MAX_SLOC_FILES
    cache_file: Path | None = None


@dataclass
class Result:
    body: dict[str, Any]
    diagnostics: dict[str, Any]
    warnings: list[str]


class UsageError(ValueError):
    pass


def parse_timestamp(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        ts = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(f"--triggered-at: not an ISO 8601 timestamp: {value!r}") from exc
    if ts.tzinfo is None:
        raise UsageError("--triggered-at must be timezone-aware (e.g. 2026-10-13T02:00:00Z)")
    return ts.astimezone(dt.timezone.utc)


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repository_from_remote(git: Git) -> str | None:
    try:
        url = git.run("config", "--get", "remote.origin.url").decode().strip()
    except GitError:
        return None
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?\Z", url)
    return m.group(1) if m else None


class _Facts:
    """Facts being assembled, plus why each null one is null."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = dict.fromkeys(FACT_ORDER)
        self.reasons: dict[str, str] = {}
        self.warnings: list[str] = []

    def set(self, name: str, value: Any) -> None:
        self.values[name] = value

    def null(self, names: Iterable[str], reason: str) -> None:
        for name in names:
            self.reasons.setdefault(name, reason)

    def unavailable(self) -> dict[str, str]:
        return {k: self.reasons.get(k, "unknown") for k in FACT_ORDER if self.values[k] is None}


def _walk_facts(f: _Facts, walk: Walk) -> None:
    if walk.count_known and walk.commits is not None:
        f.set("num_all_built_commits", len(walk.commits))
    elif walk.stop == "previous_missing":
        f.null(("num_all_built_commits",), "previous built commit not in clone")
    else:
        f.null(("num_all_built_commits",), "built-commit walk reached a shallow boundary")


def _change_facts(
    f: _Facts, git: Git, head: str, walk: Walk, cls: Classifier, max_built: int
) -> ChangeSummary | None:
    """§2 change size, summed over the built commits; tests ± from the net diff."""
    if walk.commits is None:
        f.null(DIFF_FACTS, "previous built commit not in clone")
        return None
    if not walk.diffable:
        f.null(DIFF_FACTS, "a built commit's parent is missing (shallow clone)")
        return None
    if len(walk.commits) > max_built:
        f.null(DIFF_FACTS, f"more than {max_built} built commits")
        f.warnings.append(
            f"{len(walk.commits)} built commits exceed the cap of {max_built}; "
            "change-size facts are null"
        )
        return None
    try:
        summary = summarize_commits(git, walk.commits, walk.parents, cls)
    except GitError as exc:
        f.null(DIFF_FACTS, "git diff failed")
        f.warnings.append(f"change-size facts are null: {exc}")
        return None
    f.set("src_churn", summary.src_churn)
    f.set("test_churn", summary.test_churn)
    f.set("files_added", len(summary.added))
    f.set("files_deleted", len(summary.deleted))
    f.set("files_modified", len(summary.modified))
    kinds = [cls.kind(p) for p in sorted(summary.touched)]
    f.set("src_files", sum(1 for k in kinds if k in ("src", "test")))
    f.set("doc_files", kinds.count("doc"))
    f.set("other_files", kinds.count("other"))

    # Net diff from the parent of the oldest built commit (= the previous built commit on a
    # linear walk) to the trigger commit.
    parents = walk.parents[walk.commits[-1]]
    try:
        base = parents[0] if parents else git.empty_tree()
        tc = count_test_case_changes(git, base, head, cls)
    except GitError as exc:
        f.warnings.append(f"tests_added/tests_deleted are null: {exc}")
        tc = None
    if tc is None:
        f.null(("tests_added", "tests_deleted"), "could not attribute diff lines to test files")
    else:
        f.set("tests_added", tc[0])
        f.set("tests_deleted", tc[1])
    return summary


def _codebase_facts(f: _Facts, git: Git, head: str, cls: Classifier, opts: Options) -> None:
    """§2 sloc and test density at the trigger commit (needs only the head tree)."""
    lang = cls.lang
    cache = load_cache(opts.cache_file, lang)
    try:
        counts, why, used = count_codebase(
            git, head, cls, opts.max_sloc_bytes, opts.max_sloc_files, cache
        )
    except GitError as exc:
        counts, why, used = None, f"could not read {lang.name} files at head: {exc}", set()
    if counts is None:
        f.null(SLOC_FACTS, why or "work cap exceeded")
        f.warnings.append(f"sloc facts are null: {why}")
        return
    f.set("sloc", counts.sloc)
    f.set("test_lines_per_kloc", per_kloc(counts.test_lines, counts.sloc))
    f.set("test_cases_per_kloc", per_kloc(counts.test_cases, counts.sloc))
    f.set("asserts_per_kloc", per_kloc(counts.asserts, counts.sloc))
    if counts.sloc == 0:
        f.null(SLOC_FACTS[1:], f"no non-test {lang.name} lines at head (division by zero)")
    if opts.cache_file is not None:
        try:
            save_cache(opts.cache_file, lang, {k: cache[k] for k in used})
        except OSError as exc:
            f.warnings.append(f"could not write the sloc cache: {exc}")


def _history_facts(
    f: _Facts,
    git: Git,
    head: str,
    walk: Walk,
    summary: ChangeSummary | None,
    shallow: frozenset[str],
    start: int,
) -> bool:
    """§2 file hotness, team, repo age. Returns whether the 90-day window is complete."""
    window_start = start - WINDOW_DAYS * SECONDS_PER_DAY
    built = set(walk.commits or ())
    try:
        window_ok = history_window_complete(git, head, shallow, window_start)
        if not window_ok:
            f.null(
                ("commits_on_files_touched", "team_size", "by_core_team_member"),
                "shallow clone does not cover the 90-day window",
            )
        if summary is None:
            f.null(("commits_on_files_touched",), "files touched by the build are unknown")
        elif window_ok:
            f.set(
                "commits_on_files_touched",
                commits_on_files_touched(
                    git, head, summary.history_paths, window_start, start, built
                ),
            )
        if walk.commits is None or not walk.count_known:
            f.null(("team_size", "by_core_team_member"), "built commits unknown")
        elif window_ok:
            team = team_emails(git, head, window_start, start, built)
            authors = author_emails(git, walk.commits)
            f.set("team_size", len(team))
            f.set("by_core_team_member", bool(authors) and authors <= team)
        if shallow:
            f.null(("repo_age_days",), "shallow clone: the first commit is not available")
        else:
            age = repo_age_days(git, head, start)
            f.set("repo_age_days", age)
            if age is None:
                f.null(("repo_age_days",), "first commit is later than the build start")
    except GitError as exc:
        f.warnings.append(f"history facts may be null: {exc}")
        return False
    return window_ok


def extract(opts: Options) -> Result:
    lang = LANGUAGES.get(opts.language.lower())
    if lang is None:
        raise UsageError(
            f"unsupported language {opts.language!r}; supported: {', '.join(sorted(LANGUAGES))}"
        )
    if opts.event not in ("push", "pull_request"):
        raise UsageError("--event must be push or pull_request")
    cls = Classifier(lang, opts.src_globs, opts.test_globs)
    git = Git(opts.repo)
    if not git.ok("rev-parse", "--git-dir"):
        raise UsageError(f"{opts.repo} is not a git repository")
    head = git.resolve_commit(opts.head)
    if head is None:
        raise UsageError(f"head commit {opts.head!r} is not available in this clone")

    f = _Facts()
    f.set("is_pr", opts.event == "pull_request")

    prev_given = bool(opts.previous) and not _ZERO_SHA.match(opts.previous or "")
    previous: str | None = None
    if prev_given:
        previous = git.resolve_commit(opts.previous or "")
        if previous is None:
            f.warnings.append(
                f"previous built commit {opts.previous} is not in this clone (shallow fetch or "
                "force push): the built commits are unknown, so change-size facts are null"
            )

    shallow = git.shallow_commits()
    if shallow is None:
        raise UsageError("repository reports shallow but its shallow file is unreadable")
    if shallow:
        f.warnings.append(
            "shallow clone: use actions/checkout with fetch-depth: 0; history facts that the "
            "fetched commits cannot prove are null"
        )

    triggered = (
        parse_timestamp(opts.triggered_at)
        if opts.triggered_at
        else dt.datetime.fromtimestamp(git.committer_time(head), dt.timezone.utc)
    )
    start = int(triggered.timestamp())

    walk = walk_built_commits(git, head, previous, prev_given, shallow)
    _walk_facts(f, walk)
    summary = _change_facts(f, git, head, walk, cls, opts.max_built_commits)
    _codebase_facts(f, git, head, cls, opts)
    window_ok = _history_facts(f, git, head, walk, summary, shallow, start)

    prev_out: str | None = previous
    if prev_out is None and prev_given and _HEX_SHA.match(opts.previous or ""):
        prev_out = (opts.previous or "").lower()
    body: dict[str, Any] = {
        "repository": opts.repository or repository_from_remote(git),
        "commit_sha": (opts.commit_sha or head).lower(),
        "pull_request": opts.pull_request,
        "branch": opts.branch,
        "event": opts.event,
        "triggered_at": _iso(triggered),
        "ci_run_url": opts.ci_run_url,
        "previous_built_sha": prev_out,
        "extractor": {"name": EXTRACTOR_NAME, "version": EXTRACTOR_VERSION},
        "change_facts": f.values,
    }
    diagnostics: dict[str, Any] = {
        "head": head,
        "language": lang.name,
        "walk_stop": walk.stop,
        "built_commits": len(walk.commits) if walk.commits is not None else None,
        "shallow": bool(shallow),
        "history_window_complete": window_ok,
        "unavailable": f.unavailable(),
        "warnings": f.warnings,
    }
    return Result(body, diagnostics, f.warnings)


# --------------------------------------------------------------------------- CLI


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_change_facts.py",
        description="Compute ReleaseLens change facts from git and print the API request body.",
    )
    p.add_argument("--repo", type=Path, default=Path(), help="repository path (default: .)")
    p.add_argument("--head", default="HEAD", help="trigger commit (default: HEAD)")
    p.add_argument(
        "--previous", default=None, help="previous built commit (push 'before' / PR base)"
    )
    p.add_argument("--language", default="java", help=f"one of: {', '.join(sorted(LANGUAGES))}")
    p.add_argument("--event", default="push", choices=("push", "pull_request"))
    p.add_argument(
        "--triggered-at",
        default=None,
        help="build start, ISO 8601 with timezone (default: head committer date)",
    )
    p.add_argument("--repository", default=None, help="owner/name (default: from origin URL)")
    p.add_argument("--pull-request", type=int, default=None)
    p.add_argument("--branch", default=None)
    p.add_argument("--ci-run-url", default=None)
    p.add_argument("--commit-sha", default=None, help="commit to report (default: --head)")
    p.add_argument("--src-globs", default="", help="comma/newline separated source-file globs")
    p.add_argument("--test-globs", default="", help="comma/newline separated test-file globs")
    p.add_argument("--max-built-commits", type=_positive_int, default=DEFAULT_MAX_BUILT_COMMITS)
    p.add_argument("--max-sloc-bytes", type=_positive_int, default=DEFAULT_MAX_SLOC_BYTES)
    p.add_argument("--max-sloc-files", type=_positive_int, default=DEFAULT_MAX_SLOC_FILES)
    p.add_argument("--cache-file", type=Path, default=None, help="blob-id → line-count cache")
    p.add_argument("--output", type=Path, default=None, help="body file (default: stdout)")
    p.add_argument("--diagnostics-out", type=Path, default=None)
    p.add_argument("--version", action="version", version=f"%(prog)s {EXTRACTOR_VERSION}")
    return p


def _warn(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.stderr.write(f"::warning title=ReleaseLens::{message}\n")
    else:
        sys.stderr.write(f"warning: {message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opts = Options(
        repo=args.repo,
        head=args.head,
        previous=args.previous,
        language=args.language,
        event=args.event,
        triggered_at=args.triggered_at,
        repository=args.repository or None,
        pull_request=args.pull_request,
        branch=args.branch or None,
        ci_run_url=args.ci_run_url or None,
        commit_sha=args.commit_sha or None,
        src_globs=split_globs(args.src_globs),
        test_globs=split_globs(args.test_globs),
        max_built_commits=args.max_built_commits,
        max_sloc_bytes=args.max_sloc_bytes,
        max_sloc_files=args.max_sloc_files,
        cache_file=args.cache_file,
    )
    try:
        result = extract(opts)
    except (UsageError, GitError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    for w in result.warnings:
        _warn(w)
    text = json.dumps(result.body, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    if args.diagnostics_out is not None:
        args.diagnostics_out.write_text(
            json.dumps(result.diagnostics, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
