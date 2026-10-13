from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from rl_pipeline.catalog import FINAL_2017_COLUMN_DOCS as COLUMN_DOCS
from rl_pipeline.catalog import FINAL_2017_SPEC_NAME_MAP as SPEC_NAME_MAP
from rl_pipeline.datasets import TT_5_3_2016, TT_FINAL_2017
from rl_pipeline.dictionary import build_dictionary, quality_flags, render_markdown, write_outputs
from rl_pipeline.schema import FINAL_2017_NAMES as EXPECTED_NAMES
from rl_pipeline.testing import FREE_TEXT_MARKER, build_rows, build_rows_2016

UNCERTAIN = {
    "git_merged_with",
    "gh_num_issue_comments",
    "gh_num_commit_comments",
    "gh_num_pr_comments",
}


@pytest.fixture(scope="module")
def built(converted: dict[str, Any]) -> dict[str, Any]:
    return build_dictionary(Path(converted["local_dir"]), TT_FINAL_2017)


def test_catalog_availability_rules() -> None:
    by_tag: dict[str, set[str]] = {}
    for doc in COLUMN_DOCS.values():
        by_tag.setdefault(doc.availability, set()).add(doc.name)
        assert doc.justification.strip()
        assert doc.meaning.strip()
    assert by_tag["uncertain"] == UNCERTAIN
    post = by_tag["post_build"]
    assert {"tr_status", "tr_duration"} <= post
    assert {n for n in EXPECTED_NAMES if n.startswith("tr_log_")} <= post
    assert post == {"tr_status", "tr_duration"} | {
        n for n in EXPECTED_NAMES if n.startswith("tr_log_")
    }
    assert by_tag["pre_build"] == set(EXPECTED_NAMES) - post - UNCERTAIN
    for name in UNCERTAIN:
        assert COLUMN_DOCS[name].role == "excluded"
    for doc in COLUMN_DOCS.values():
        if doc.role in {"identifier", "context"}:
            assert "not a model feature" in doc.justification
    assert {d.name for d in COLUMN_DOCS.values() if d.grain == "job"} == {"tr_job_id"} | {
        n for n in EXPECTED_NAMES if n.startswith("tr_log_")
    }


def test_spec_map_targets_exist() -> None:
    assert set(SPEC_NAME_MAP.values()) <= set(EXPECTED_NAMES)


def test_counts_match_fixture(built: dict[str, Any]) -> None:
    rows = build_rows()
    java = [r for r in rows if r["gh_lang"] == "java"]
    counts = built["counts"]
    assert counts["overall"] == {
        "jobs": len(rows),
        "distinct_job_ids": len(rows),
        "builds": len({r["tr_build_id"] for r in rows}),
        "projects": len({r["gh_project_name"] for r in rows}),
    }
    assert counts["java"]["jobs"] == len(java)
    assert counts["java"]["builds"] == len({r["tr_build_id"] for r in java})
    labels = Counter({r["tr_build_id"]: r["tr_status"] for r in java}.values())
    assert counts["java_label_distribution"] == dict(labels)
    starts = sorted(r["gh_build_started_at"] for r in java)
    assert counts["java_build_started_at_range"] == {
        "column": "gh_build_started_at",
        "min": starts[0],
        "max": starts[-1],
    }


def test_columns_profiled_at_declared_grain(built: dict[str, Any]) -> None:
    cols = {c["name"]: c for c in built["columns"]}
    assert list(cols) == list(EXPECTED_NAMES)
    java = [r for r in build_rows() if r["gh_lang"] == "java"]
    n_builds = len({r["tr_build_id"] for r in java})
    assert cols["gh_team_size"]["java_profile"]["rows_measured"] == n_builds
    assert cols["tr_log_status"]["java_profile"]["rows_measured"] == len(java)
    assert cols["gh_num_commits_in_push"]["java_profile"]["null_rate"] == 1.0
    assert cols["gh_num_commits_in_push"]["quality_flags"] == ["all_null"]
    assert cols["git_diff_test_churn"]["quality_flags"] == ["constant"]
    # build-level columns never vary within a build in the fixture; job ids do
    assert cols["gh_team_size"]["java_builds_with_varying_values"] == 0
    assert cols["tr_job_id"]["java_builds_with_varying_values"] > 0
    assert cols["git_merged_with"]["availability"] == "uncertain"
    assert cols["git_merged_with"]["model_input_default"] is False
    assert cols["tr_build_id"]["model_input_default"] is False
    assert cols["git_diff_src_churn"]["model_input_default"] is True
    # measured all-null / constant feature candidates are not model inputs by default
    assert cols["gh_num_commits_in_push"]["model_input_default"] is False
    assert cols["git_diff_test_churn"]["model_input_default"] is False


def test_invariants_and_extra_evidence(built: dict[str, Any]) -> None:
    rows = build_rows()
    java_builds = {r["tr_build_id"]: r for r in rows if r["gh_lang"] == "java"}
    inv = built["invariants"]
    pr = inv["pr_created_after_build_start"]
    assert pr["of"] == sum(r["gh_is_pr"] == "TRUE" for r in java_builds.values())
    assert pr["violations"] == 0  # the generator opens every PR 5 h before its build
    assert pr["note"] == ""
    prev = inv["prev_build_not_earlier"]
    assert prev["of"] == sum(r["tr_prev_build"] != "NA" for r in java_builds.values())
    assert prev["violations"] == 0
    master = built["evidence"]["gh_repo_num_commits"]["master_push_builds"]
    assert set(master) == {"method", "non_decreasing_share", "strictly_increasing_share", "pairs"}
    md = render_markdown(built)
    assert "## Invariant checks (Java, measured)" in md
    assert "Not run for this build of the dictionary" in md  # no crosscheck_dir given


def test_all_null_flag_uses_exact_counts() -> None:
    # 1 value in 3,000,000 rows rounds to null_rate 1.0 but is not "all null"
    almost = {"null_rate": 1.0, "null_count": 2_999_999, "rows_measured": 3_000_000}
    assert quality_flags({**almost, "distinct_count": 1}, "outcome") == ["constant"]
    empty = {"null_rate": 1.0, "null_count": 5, "rows_measured": 5, "distinct_count": 0}
    assert quality_flags(empty, "outcome") == ["all_null"]


def test_quality_flags_rules() -> None:
    base = {"null_rate": 0.0, "distinct_count": 10, "zero_rate": 0.5}
    assert quality_flags(base, "feature_candidate") == []
    assert quality_flags({**base, "zero_rate": 0.96}, "feature_candidate") == ["mostly_zero"]
    assert quality_flags({**base, "distinct_count": 1}, "identifier") == []
    assert quality_flags({**base, "null_rate": 1.0, "distinct_count": 0}, "outcome") == ["all_null"]


def test_outputs_are_example_free_and_deterministic(
    built: dict[str, Any], converted: dict[str, Any], tmp_path: Path
) -> None:
    md_a, js_a = tmp_path / "a.md", tmp_path / "a.json"
    write_outputs(built, md_a, js_a)
    again = build_dictionary(Path(converted["local_dir"]), TT_FINAL_2017)
    md_b, js_b = tmp_path / "b.md", tmp_path / "b.json"
    write_outputs(again, md_b, js_b)
    assert md_a.read_bytes() == md_b.read_bytes()
    assert js_a.read_bytes() == js_b.read_bytes()
    for path in (md_a, js_a):
        text = path.read_text()
        assert FREE_TEXT_MARKER not in text
        assert "synthetic-org/" not in text  # no project names
    data = json.loads(js_a.read_text())
    assert len(data["columns"]) == 66
    assert data["dataset"]["license"] == "CC BY 4.0"
    md = md_a.read_text()
    assert "## Spec names → real 2017 column names" in md
    assert "`gh_src_churn` | `git_diff_src_churn`" in md
    assert "excluded from model inputs by default" in md
    assert "## rl_core build inputs → columns" in md
    assert render_markdown(built) == md


def test_requires_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_dictionary(tmp_path, TT_FINAL_2017)


def test_rejects_a_conversion_of_another_layout(converted: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="schema"):
        build_dictionary(Path(converted["local_dir"]), TT_5_3_2016)


# ---------------------------------------------------------------- primary (5_3_2016) layout
@pytest.fixture(scope="module")
def built_2016(converted_2016: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Any:
    archive = tmp_path_factory.mktemp("archive")
    rows = build_rows_2016()
    java = {r["tr_build_id"]: r for r in rows if r["gh_lang"] == "java"}
    # a synthetic stand-in for the monperrus archive: two projects, explicit-UTC start times;
    # one build shifted by +1 h, one with a different status, one absent from the dump
    ids = sorted(java, key=int)
    lines = ["build_id,commit,pull_req,branch,status,duration,started_at,jobs,event_type"]
    for i, b in enumerate(ids):
        r = java[b]
        st = r["tr_started_at"]
        if i == 0:
            st = st.replace(st[11:13], f"{int(st[11:13]) + 1:02d}", 1)
        status = "errored" if i == 1 and r["tr_status"] != "errored" else r["tr_status"]
        lines.append(f"{b},x,,master,{status},{r['tr_duration']},{st} UTC,[],push")
    lines.append("999999,x,,master,passed,1,2014-01-01 00:00:00 UTC,[],push")
    (archive / "org@one").mkdir()
    (archive / "org@one" / "repo-data-travis.csv").write_text("\n".join(lines) + "\n")
    return build_dictionary(Path(converted_2016["local_dir"]), TT_5_3_2016, crosscheck_dir=archive)


def test_2016_counts_and_build_rule(built_2016: dict[str, Any]) -> None:
    rows = build_rows_2016()
    java = [r for r in rows if r["gh_lang"] == "java"]
    c = built_2016["counts"]
    assert c["java"]["jobs"] == len(java)
    assert c["java"]["distinct_job_ids"] == len({r["tr_job_id"] for r in java}) == len(java) - 2
    assert c["java"]["builds"] == len({r["tr_build_id"] for r in java})
    assert c["java_build_started_at_range"]["column"] == "tr_started_at"
    assert "listed in the build's own tr_jobs first" in built_2016["measurement"]["build_grain"]
    # the representative row of each build is one of its own jobs (never the foreign row)
    labels = Counter({r["tr_build_id"]: r["tr_status"] for r in java}.values())
    assert c["java_label_distribution"] == dict(labels)


def test_2016_invariants_and_evidence(built_2016: dict[str, Any]) -> None:
    inv = built_2016["invariants"]
    assert inv["job_id_unique"]["violations"] == 2
    assert inv["job_listed_in_own_build"]["violations"] == 2
    assert inv["num_jobs_matches_list"]["violations"] == 0
    java = [r for r in build_rows_2016() if r["gh_lang"] == "java"]
    placeholders = sum(r["tr_prev_build"] in {"-1", "-1.0"} for r in java)
    assert placeholders > 0
    assert inv["prev_build_placeholder"]["violations"] == placeholders
    assert inv["first_commit_before_start"]["violations"] > 0  # BST months in the fixture
    ev = built_2016["evidence"]
    dup = ev["duplicate_job_rows"]
    assert dup["job_ids_in_more_than_one_build"] == 2
    assert dup["duplicated_ids_listed_on_exactly_one_row"] == 2
    gc = ev["git_commits"]
    assert gc["num_commits_equals_entries"] == gc["rows_measured"]


def test_2016_crosscheck(built_2016: dict[str, Any]) -> None:
    cc = built_2016["utc_crosscheck"]
    n_java = built_2016["counts"]["java"]["builds"]
    assert cc["archive"] == Path(cc["archive"]).name  # no absolute paths in the output
    assert cc["archive_rows"] == n_java + 1
    assert cc["joined_java_builds"] == n_java
    assert cc["started_at_equal"] == n_java - 1
    assert cc["started_at_off_by_1h_or_2h"] == 1
    assert cc["status_equal"] == n_java - 1


def test_2016_rendering(built_2016: dict[str, Any], tmp_path: Path) -> None:
    md, js = tmp_path / "d.md", tmp_path / "d.json"
    write_outputs(built_2016, md, js)
    text = md.read_text()
    assert text.startswith("# Data dictionary — TravisTorrent (Figshare `travistorrent_5_3_2016`)")
    assert "`docs/data/data_dictionary.json`" in text
    assert "## Spec names → real 2016 column names" in text
    assert "## Evidence (measured on the Java subset)" in text
    assert "## UTC and status cross-check (secondary archive)" in text
    assert "| `num_commits_in_push` | — | none |" in text
    assert FREE_TEXT_MARKER not in text
    assert "synthetic-org/" not in text
    data = json.loads(js.read_text())
    assert len(data["columns"]) == 56
    assert {m["field"] for m in data["build_input_map"] if m["status"] == "none"} == {
        "num_commits_in_push",
        "repo_age_days",
    }
