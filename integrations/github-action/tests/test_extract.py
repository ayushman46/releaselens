"""Exact change facts on throwaway git repositories (docs/data/FEATURE_DEFINITIONS.md §1-§2)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from gitrepo import ALICE, BOB, DAY, T0, Repo, iso

import extract_change_facts as ecf
from rl_core.features.change import BUILD_INPUT_FIELDS

SCRIPT = Path(ecf.__file__).resolve()
SRC = "src/main/java/a"
TST = "src/test/java/a"

A_V1 = """package a;

public class A {
    int x = 1;
}
"""
A_V2 = """package a;

public class A {
    int x = 2;
    int y = 3;
}
"""  # vs A_V1: 2 lines added, 1 deleted
A_PLUS = """package a;

public class A {
    int x = 1;
    int z = 0;
}
"""  # vs A_V1: 1 line added
A_TEST = """package a;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class ATest {
    @Test
    public void addsUp() {
        assertEquals(2, 1 + 1);
    }

    @Test
    public void subtracts() {
        assertEquals(0, 1 - 1);
    }
}
"""  # 17 lines, 13 code lines, 2 test cases, 2 asserts

# Comment stripping: 17 code lines. Strings hide //, /* and */; text blocks span lines.
SLOC_SAMPLE = r'''/*
 * License header
 */
package a; // trailing comment

import java.util.List; /* inline */

/** Javadoc
 *  with // slashes inside
 */
public class S {
    String url = "http://example.com/*not-a-comment*/"; // c
    String t = "a\"b // still string";
    char q = '"';
    char s = '\'';
    String open = "/*";
    int a = 1;
    String close = "*/";
    /* block */ int x = 1; /* another */
    int y = 2; /* starts here
    still comment
    */ int z = 3;
    String block = """
        text // with slashes
        """;

    // whole-line comment
}
'''
# 11 code lines, 3 test-case lines, 4 assert lines
XTEST_SAMPLE = """public class XTest {
    @Test
    public void a() {
        assertEquals(1, 1);
        assertThat(x).isEqualTo(1);
        // assertTrue(false);
        String s = "assertFalse(";
        assert x > 0;
    }
    public void testB() { Assertions.assertTrue(true); }
    @Test public void testC() {}
    /* @Test */
}
"""


def run(
    repo: Repo, head: str, previous: str | None = None, at: int | None = None, **kw: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    opts = ecf.Options(
        repo=repo.path,
        head=head,
        previous=previous,
        triggered_at=iso(at) if at is not None else None,
        repository="acme/demo",
        **kw,
    )
    result = ecf.extract(opts)
    return result.body["change_facts"], result.diagnostics


# --------------------------------------------------------------------------- §1 + §2 scenarios


def test_single_commit_with_src_and_test_changes(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write("README.md", "hello\n")
    r.write(f"{SRC}/A.java", A_V1)
    r.commit("c1", T0)
    r.write(f"{SRC}/A.java", A_V2)
    r.write(f"{TST}/ATest.java", A_TEST)
    r.write("pom.xml", "<project/>\n")
    head = r.commit("c2", T0 + DAY)

    facts, diag = run(r, head, at=T0 + DAY + 3600)

    assert facts == {
        "src_churn": 3,
        "test_churn": 17,
        "files_added": 2,
        "files_deleted": 0,
        "files_modified": 1,
        "tests_added": 2,
        "tests_deleted": 0,
        "src_files": 2,
        "doc_files": 0,
        "other_files": 1,
        "sloc": 5,
        "test_lines_per_kloc": 2600.0,
        "test_cases_per_kloc": 400.0,
        "asserts_per_kloc": 400.0,
        "num_all_built_commits": 1,
        "team_size": 1,
        "by_core_team_member": True,
        "is_pr": False,
        "commits_on_files_touched": 1,
        "repo_age_days": 1,
    }
    assert diag["walk_stop"] == "no_previous_build"
    assert diag["unavailable"] == {}


def test_multi_commit_push_since_previous_build(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.write(f"{SRC}/B.java", "package a;\n\npublic class B {\n    void b() {}\n}\n")
    r.write("README.md", "hello\n")
    r.commit("c1", T0)
    r.write("README.md", "hello\nworld\n")
    prev = r.commit("c2 (previous build)", T0 + DAY, author=BOB)
    r.write(f"{SRC}/C.java", "package a;\nclass C {\n}\n")  # +3
    r.commit("c3", T0 + 2 * DAY)
    r.write(f"{SRC}/C.java", "package a;\nfinal class C {\n}\n")  # +1 -1
    r.write(f"{SRC}/A.java", A_PLUS)  # +1
    r.commit("c4", T0 + 3 * DAY)
    r.delete(f"{SRC}/B.java")  # -5
    r.write("docs/guide.md", "# Guide\n\nhello\n")  # documentation: not churn
    head = r.commit("c5", T0 + 4 * DAY)

    facts, diag = run(r, head, prev, at=T0 + 5 * DAY)

    assert diag["walk_stop"] == "build_found"
    assert facts["num_all_built_commits"] == 3
    assert facts["src_churn"] == 3 + 2 + 1 + 5  # C.java counted in both commits that touched it
    assert facts["test_churn"] == 0
    # unique paths per status: C.java was added (c3) and modified (c4), so it is in both sets
    assert (facts["files_added"], facts["files_modified"], facts["files_deleted"]) == (2, 2, 1)
    assert (facts["src_files"], facts["doc_files"], facts["other_files"]) == (3, 1, 0)
    assert (facts["tests_added"], facts["tests_deleted"]) == (0, 0)
    assert facts["commits_on_files_touched"] == 1  # c1 (A.java, B.java); c2 only touched README
    assert facts["team_size"] == 2  # alice (c1) and bob (c2); built commits excluded
    assert facts["by_core_team_member"] is True


def test_walk_stops_at_merge_commit_which_is_included(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    c1 = r.commit("c1", T0)
    r.checkout("-b", "feature")
    r.write(f"{SRC}/F.java", "package a;\n\nclass F {\n}\n")  # +4, arrives through the merge
    r.commit("f1", T0 + DAY)
    r.checkout("main")
    r.write(f"{SRC}/A.java", A_PLUS)  # +1, before the merge: not part of this build
    r.commit("c2", T0 + 2 * DAY)
    merge = r.merge("feature", T0 + 3 * DAY)
    r.write(f"{SRC}/G.java", "package a;\nclass G {}\n")  # +2
    head = r.commit("c3", T0 + 4 * DAY)

    facts, diag = run(r, head, c1, at=T0 + 5 * DAY)
    assert diag["walk_stop"] == "merge_found"
    assert facts["num_all_built_commits"] == 2  # c3 and the merge; the walk stops there
    assert facts["src_churn"] == 2 + 4  # the merge is diffed against its first parent
    assert (facts["files_added"], facts["files_modified"]) == (2, 0)

    facts, diag = run(r, merge, c1, at=T0 + 5 * DAY)
    assert diag["walk_stop"] == "merge_found"
    assert facts["num_all_built_commits"] == 1
    assert facts["src_churn"] == 4


def test_renames_count_in_none_of_added_deleted_modified(tmp_path: Path) -> None:
    b10 = "package a;\nclass B10 {\n" + "".join(f"    int a{i};\n" for i in range(1, 8)) + "}\n"
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.write(f"{SRC}/B10.java", b10)
    r.write("README.md", "hello\n")
    r.commit("c1", T0)
    r.git("mv", f"{SRC}/A.java", f"{SRC}/Renamed.java")  # pure rename
    r.delete(f"{SRC}/B10.java")
    r.write("lib/B10Moved.java", b10.replace("int a4;", "int a4 = 4;"))  # rename + 1 line edit
    r.write("README.md", "hello\nagain\n")
    head = r.commit("c2", T0 + DAY)

    facts, _ = run(r, head, at=T0 + 2 * DAY)
    assert (facts["files_added"], facts["files_deleted"], facts["files_modified"]) == (0, 0, 1)
    assert facts["src_churn"] == 2  # the pure rename adds nothing; the edited one +1 -1
    assert (facts["src_files"], facts["doc_files"], facts["other_files"]) == (2, 1, 0)


def test_binary_files_count_zero_churn(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write("README.md", "hello\n")
    r.commit("c1", T0)
    r.write(f"{SRC}/Blob.java", b"\x00\x01binary\x00\xff\x00")
    r.write("lib/x.jar", bytes(range(256)))
    r.write(f"{SRC}/T.java", "package a;\nclass T {}\n")
    head = r.commit("c2", T0 + DAY)

    facts, _ = run(r, head, at=T0 + 2 * DAY)
    assert facts["src_churn"] == 2  # numstat "-" for Blob.java counts 0
    assert facts["files_added"] == 3
    assert (facts["src_files"], facts["doc_files"], facts["other_files"]) == (2, 0, 1)
    assert facts["sloc"] == 2  # binary blob contributes no lines


def test_test_cases_added_and_deleted(tmp_path: Path) -> None:
    calc_v1 = """package a;

import org.junit.Test;

public class CalcTest {
    @Test
    public void one() { assertTrue(true); }

    @Test
    public void two() { assertTrue(true); }

    @Test
    public void three() { assertTrue(true); }
}
"""
    calc_v2 = """package a;

import org.junit.Test;

public class CalcTest {
    @Test
    public void one() { assertTrue(true); }

    @Test
    public void two() { assertTrue(true); }

    public void testFour() { assertTrue(true); }

    @Test public void testFive() {}

    // @Test disabled for now
    /* @Test */
    String marker = "@Test";
}
"""
    old_test = (
        "package a;\n\npublic class OldTest {\n"
        "    @Test public void a() {}\n    @Test public void b() {}\n}\n"
    )
    r = Repo(tmp_path / "r")
    r.write(f"{TST}/CalcTest.java", calc_v1)
    r.write(f"{TST}/OldTest.java", old_test)
    prev = r.commit("c1", T0)
    r.write(f"{TST}/CalcTest.java", calc_v2)
    r.delete(f"{TST}/OldTest.java")
    # not counted: a non-test Java file, and a test file of another language
    r.write(f"{SRC}/Main.java", "class Main {\n    @Test\n    public void testHelper() {}\n}\n")
    r.write("web/tests/app.js", "function testX() {}\n// @Test\n")
    head = r.commit("c2", T0 + DAY)

    facts, _ = run(r, head, prev, at=T0 + 2 * DAY)
    # added: testFour(, the one-line "@Test ... testFive(" (one line counts once)
    # deleted: one "@Test" in CalcTest, two lines in the deleted OldTest
    assert (facts["tests_added"], facts["tests_deleted"]) == (2, 3)
    assert facts["files_deleted"] == 1


def test_sloc_comment_and_string_stripping() -> None:
    java = ecf.JAVA
    assert java.count(SLOC_SAMPLE.encode()) == ecf.BlobCounts(17, 0, 0)
    assert java.count(SLOC_SAMPLE.replace("\n", "\r\n").encode()) == ecf.BlobCounts(17, 0, 0)
    assert java.count(XTEST_SAMPLE.encode()) == ecf.BlobCounts(11, 3, 4)
    assert java.count(b"") == ecf.BlobCounts(0, 0, 0)
    assert java.count(b"/* never closed\nint x;\n") == ecf.BlobCounts(0, 0, 0)


def test_sloc_and_density_at_head(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/S.java", SLOC_SAMPLE)
    r.write(f"{TST}/XTest.java", XTEST_SAMPLE)
    r.write(f"{SRC}/notes.txt", "// not java\n")
    head = r.commit("c1", T0)

    facts, _ = run(r, head, at=T0 + DAY)
    assert facts["sloc"] == 17
    assert facts["test_lines_per_kloc"] == 647.0588  # 11 / 17 * 1000
    assert facts["test_cases_per_kloc"] == 176.4706  # 3 / 17 * 1000
    assert facts["asserts_per_kloc"] == 235.2941  # 4 / 17 * 1000


def test_commits_on_files_touched_window_excludes_old_and_built(tmp_path: Path) -> None:
    t = T0 + 200 * DAY
    hot, cold = f"{SRC}/Hot.java", f"{SRC}/Cold.java"
    r = Repo(tmp_path / "r")

    def touch(path: str, n: int, days_before: int) -> str:
        r.write(path, f"class X {{ int v = {n}; }}\n")
        return r.commit(f"touch {path} {n}", t - days_before * DAY)

    r.write(cold, "class Cold {}\n")
    touch(hot, 1, 150)  # outside the 90-day window
    touch(hot, 2, 100)  # outside
    touch(hot, 3, 60)  # counted
    touch(cold, 4, 30)  # inside, but Cold.java is not touched by the build
    prev = touch(hot, 5, 10)  # counted (the previous build is not one of this build's commits)
    touch(hot, 6, 2)  # built
    head = touch(hot, 7, 1)  # built

    facts, _ = run(r, head, prev, at=t)
    assert facts["num_all_built_commits"] == 2
    assert facts["commits_on_files_touched"] == 2


def test_team_size_two_authors_with_mailmap(tmp_path: Path) -> None:
    t = T0 + 100 * DAY
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.commit("c1", t - 120 * DAY, author="Carol <carol@example.com>")  # outside the window
    r.write(f"{SRC}/A.java", A_PLUS)
    r.commit("c2", t - 50 * DAY, author=ALICE)
    r.write(f"{SRC}/A.java", A_V2)
    r.commit("c3", t - 40 * DAY, author=BOB)
    r.write("README.md", "x\n")
    r.commit("c3b", t - 35 * DAY, author="Bob <BOB@Example.com>")  # same person, other case
    r.write(".mailmap", "Alice <alice@example.com> <alice.home@example.org>\n")
    r.write(f"{SRC}/A.java", A_V1)
    prev = r.commit("c4", t - 30 * DAY, author="Alice Home <alice.home@example.org>")
    r.write(f"{SRC}/A.java", A_PLUS)
    head = r.commit("c5", t - DAY, author=ALICE)
    r.checkout("-b", "dave", prev)
    r.write(f"{SRC}/A.java", A_V2)
    dave = r.commit("c5 by dave", t - DAY, author="Dave <dave@example.com>")

    facts, _ = run(r, head, prev, at=t)
    assert facts["team_size"] == 2  # alice (two emails, mailmap), bob (two cases)
    assert facts["by_core_team_member"] is True

    facts, _ = run(r, dave, prev, at=t)
    assert facts["team_size"] == 2  # dave's own built commit does not make him a member
    assert facts["by_core_team_member"] is False


def _history_repo(tmp_path: Path, dates: list[int]) -> tuple[Repo, list[str]]:
    r = Repo(tmp_path / "origin")
    shas = []
    for i, when in enumerate(dates):
        r.write(f"{SRC}/A.java", A_V1.replace("1;", f"{i};"))
        r.write(f"{TST}/ATest.java", A_TEST)
        shas.append(r.commit(f"c{i}", when))
    return r, shas


def test_shallow_clone_history_facts_are_null(tmp_path: Path) -> None:
    origin, shas = _history_repo(tmp_path, [T0, T0 + DAY, T0 + 2 * DAY])
    start = T0 + 3 * DAY

    depth1 = origin.clone_shallow(tmp_path / "d1", 1)
    facts, diag = run(depth1, shas[2], at=start)
    assert diag["shallow"] is True
    assert facts["num_all_built_commits"] == 1  # no previous build: head only, by definition
    for name in ecf.DIFF_FACTS:  # head's parent was not fetched
        assert facts[name] is None, name
    for name in ("commits_on_files_touched", "team_size", "by_core_team_member", "repo_age_days"):
        assert facts[name] is None, name
    assert facts["sloc"] == 4  # only needs the head tree
    assert facts["is_pr"] is False

    depth2 = origin.clone_shallow(tmp_path / "d2", 2)
    facts, diag = run(depth2, shas[2], shas[1], at=start)
    assert facts["num_all_built_commits"] == 1
    assert facts["src_churn"] == 2  # head vs its (fetched) parent
    assert diag["history_window_complete"] is False
    for name in ("commits_on_files_touched", "team_size", "by_core_team_member", "repo_age_days"):
        assert facts[name] is None, name

    # previous build outside the clone: the built commits are unknown
    facts, diag = run(depth1, shas[2], shas[0], at=start)
    assert diag["walk_stop"] == "previous_missing"
    assert facts["num_all_built_commits"] is None
    assert facts["src_churn"] is None


def test_shallow_clone_that_covers_the_window_keeps_team_facts(tmp_path: Path) -> None:
    origin, shas = _history_repo(tmp_path, [T0, T0 + 10 * DAY, T0 + 200 * DAY])
    clone = origin.clone_shallow(tmp_path / "d2", 2)  # boundary commit is 191 days old
    facts, diag = run(clone, shas[2], shas[1], at=T0 + 201 * DAY)
    assert diag["history_window_complete"] is True
    assert facts["team_size"] == 0  # the only commit in the window is the built one
    assert facts["commits_on_files_touched"] == 0
    assert facts["repo_age_days"] is None  # the first commit is still unknown


def test_no_previous_build_uses_only_head(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.commit("c1", T0)
    r.write(f"{SRC}/A.java", A_PLUS)
    r.commit("c2", T0 + DAY)
    r.write(f"{SRC}/A.java", A_V2)
    head = r.commit("c3", T0 + 2 * DAY)

    for previous in (None, "0" * 40):
        facts, diag = run(r, head, previous, at=T0 + 3 * DAY)
        assert diag["walk_stop"] == "no_previous_build"
        assert facts["num_all_built_commits"] == 1
        assert facts["src_churn"] == 4  # c3 only: A_PLUS -> A_V2 is +2 -2


def test_previous_commit_missing_makes_built_set_unknown(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    head = r.commit("c1", T0)
    facts, diag = run(r, head, "ab" * 20, at=T0 + DAY)
    assert diag["walk_stop"] == "previous_missing"
    for name in (*ecf.DIFF_FACTS, "num_all_built_commits", "team_size", "by_core_team_member"):
        assert facts[name] is None, name
    assert facts["commits_on_files_touched"] is None
    assert facts["sloc"] == 4
    assert facts["repo_age_days"] == 1


def test_pull_request_walks_from_pr_head_excluding_the_base(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.commit("c1", T0)
    r.checkout("-b", "feature")
    r.write(f"{TST}/ATest.java", A_TEST)  # 2 test cases
    r.commit("p1", T0 + 2 * DAY)
    r.write(f"{SRC}/A.java", A_PLUS)
    pr_head = r.commit("p2", T0 + 3 * DAY)
    r.checkout("main")
    r.write(f"{TST}/OtherTest.java", "class OtherTest {\n    @Test public void x() {}\n}\n")
    base = r.commit("c2 (base moved on)", T0 + 4 * DAY)

    facts, _ = run(r, pr_head, base, at=T0 + 5 * DAY, event="pull_request", pull_request=7)
    assert facts["is_pr"] is True
    assert facts["num_all_built_commits"] == 2
    assert (facts["src_churn"], facts["test_churn"]) == (1, 17)
    assert (facts["tests_added"], facts["tests_deleted"]) == (2, 0)  # base changes not counted


def test_work_caps_turn_facts_null_not_partial(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    prev = r.commit("c1", T0)
    r.write(f"{SRC}/B.java", "class B {}\n")
    r.commit("c2", T0 + DAY)
    r.write(f"{SRC}/C.java", "class C {}\n")
    head = r.commit("c3", T0 + 2 * DAY)

    facts, diag = run(r, head, prev, at=T0 + 3 * DAY, max_built_commits=1, max_sloc_files=2)
    assert facts["num_all_built_commits"] == 2  # still exact
    assert facts["src_churn"] is None
    assert facts["commits_on_files_touched"] is None
    assert facts["sloc"] is None  # 3 Java files > cap of 2
    assert "sloc" in diag["unavailable"]


def test_no_source_in_the_language_makes_size_facts_unknown_not_zero(tmp_path: Path) -> None:
    """Seen live: the Java extractor on a Python repository reported sloc 0, which no training
    build has, and that one value drove the score to HIGH."""
    r = Repo(tmp_path / "r")
    r.write("README.md", "hello\n")
    head = r.commit("c1", T0)
    facts, diag = run(r, head, at=T0)
    assert facts["sloc"] is None
    assert facts["test_lines_per_kloc"] is None
    assert "no non-test java lines" in diag["unavailable"]["sloc"].lower()
    assert any("outside the risk model's training domain" in w for w in diag["warnings"])


def test_blob_cache_is_keyed_by_blob_id_and_counter_version(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.write(f"{SRC}/Copy.java", A_V1)  # same blob: counted once
    head = r.commit("c1", T0)
    cache = tmp_path / "cache.json"

    first, _ = run(r, head, at=T0, cache_file=cache)
    second, _ = run(r, head, at=T0, cache_file=cache)
    assert first == second
    assert first["sloc"] == 8
    stored = json.loads(cache.read_text())
    blob = r.git("rev-parse", f"HEAD:{SRC}/A.java")
    assert stored == {"counter": f"java:{ecf.COUNTER_VERSION}", "blobs": {blob: [4, 0, 0]}}

    stored["blobs"][blob] = [100, 0, 0]  # a cached count is trusted for that blob id
    cache.write_text(json.dumps(stored))
    assert run(r, head, at=T0, cache_file=cache)[0]["sloc"] == 200
    stored["counter"] = "java:0"  # another counter version is ignored
    cache.write_text(json.dumps(stored))
    assert run(r, head, at=T0, cache_file=cache)[0]["sloc"] == 8


# --------------------------------------------------------------------------- contract + CLI


def test_change_fact_keys_are_build_input_fields(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    head = r.commit("c1", T0)
    result = ecf.extract(ecf.Options(repo=r.path, head=head))
    assert set(result.body["change_facts"]) <= set(BUILD_INPUT_FIELDS)
    assert set(ecf.FACT_ORDER) <= set(BUILD_INPUT_FIELDS)
    assert "description_complexity" not in result.body["change_facts"]  # excluded (uncertain)
    assert set(result.body) == {
        "repository",
        "commit_sha",
        "pull_request",
        "branch",
        "event",
        "triggered_at",
        "ci_run_url",
        "previous_built_sha",
        "extractor",
        "change_facts",
    }
    assert result.body["triggered_at"] == iso(T0)  # default: head committer date


def _cli_repo(tmp_path: Path) -> tuple[list[str], str, str]:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    prev = r.commit("c1", T0)
    r.write(f"{SRC}/A.java", A_V2)
    r.write(f"{TST}/ATest.java", A_TEST)
    head = r.commit("c2", T0 + DAY)
    args = [
        "--repo", str(r.path), "--head", head, "--previous", prev, "--event", "pull_request",
        "--pull-request", "12", "--triggered-at", "2026-01-03T00:00:00+00:00",
        "--repository", "acme/demo", "--branch", "feature/x",
    ]  # fmt: skip
    return args, head, prev


def _cli(python: str, args: list[str]) -> str:
    proc = subprocess.run([python, str(SCRIPT), *args], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_cli_output_is_deterministic(tmp_path: Path) -> None:
    args, head, prev = _cli_repo(tmp_path)
    out = _cli(sys.executable, args)
    assert out == _cli(sys.executable, args)
    body = json.loads(out)
    assert body["commit_sha"] == head
    assert body["previous_built_sha"] == prev
    assert body["pull_request"] == 12
    assert body["triggered_at"] == "2026-01-03T00:00:00Z"
    assert body["extractor"] == {"name": "releaselens-action", "version": ecf.EXTRACTOR_VERSION}
    assert body["change_facts"]["is_pr"] is True


def test_cli_runs_unchanged_on_python39(tmp_path: Path) -> None:
    """The action promises Python 3.9+ with the standard library only."""
    py39 = _python39()
    if py39 is None:
        pytest.skip("no Python 3.9 interpreter on this machine")
    args, _, _ = _cli_repo(tmp_path)
    assert _cli(py39, args) == _cli(sys.executable, args)


def _python39() -> str | None:
    for candidate in ("python3.9", "/usr/bin/python3"):
        path = shutil.which(candidate)
        if path is None:
            continue
        version = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if version == "True":
            return path
    return None


def test_cli_usage_errors_exit_2(tmp_path: Path) -> None:
    r = Repo(tmp_path / "r")
    r.write(f"{SRC}/A.java", A_V1)
    r.commit("c1", T0)
    for extra in (["--language", "cobol"], ["--head", "f" * 40], ["--triggered-at", "yesterday"]):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(r.path), *extra],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2, extra
        assert proc.stdout == ""


# --------------------------------------------------------------------------- units


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("src/main/java/a/A.java", "src"),
        ("src/test/java/a/AnythingHelper.java", "test"),
        ("core/src/main/java/a/FooTest.java", "test"),
        ("core/src/main/java/a/FooTests.java", "test"),
        ("core/src/main/java/a/TestFoo.java", "test"),
        ("core/src/main/java/a/Contest.java", "src"),  # "test" substring is not a test dir
        ("latest/Foo.java", "src"),
        ("tests/helpers.py", "test"),
        ("scripts/build.sh", "src"),
        ("Makefile", "src"),
        ("README", "doc"),
        ("docs/index.md", "doc"),
        ("web/site.css", "doc"),
        ("pom.xml", "other"),
        ("build.gradle", "other"),
        ("config/app.yml", "other"),
        ("lib/x.jar", "other"),
    ],
)
def test_default_classification(path: str, kind: str) -> None:
    assert ecf.Classifier(ecf.JAVA).kind(path) == kind


def test_glob_overrides() -> None:
    cls = ecf.Classifier(ecf.JAVA, src_globs=["app/**/*.java"], test_globs=["*Spec.java"])
    assert cls.kind("app/x/y/Z.java") == "src"
    assert cls.kind("lib/Other.java") == "other"  # outside src-globs: not code
    assert cls.kind("lib/FooSpec.java") == "test"
    assert cls.kind("app/src/test/java/Z.java") == "src"  # default test rules replaced
    assert ecf.split_globs("a/**, b/*.java\nc") == ("a/**", "b/*.java", "c")


def test_patch_parser_never_mistakes_removed_lines_for_headers() -> None:
    patch = (
        b"diff --git a/x b/x\n"
        b"index 1111111..2222222 100644\n"
        b"--- a/x\n"
        b"+++ b/x\n"
        b"@@ -1,2 +1 @@\n"
        b"---i;\n"
        b"--- a/fake\n"
        b"+++ b/fake\n"
        b"\\ No newline at end of file\n"
        b"diff --git a/y b/y\n"
        b"@@ -0,0 +1 @@\n"
        b"+@Test\n"
    )
    assert list(ecf._patch_lines(patch)) == [
        (0, "-", b"--i;"),
        (0, "-", b"-- a/fake"),
        (0, "+", b"++ b/fake"),
        (1, "+", b"@Test"),
    ]
