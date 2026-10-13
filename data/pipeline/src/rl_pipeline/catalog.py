"""Hand-curated column knowledge: meaning, grain, role and availability (pre/post build).

Shared types live here, together with the catalog of the **secondary** release
``final-2017-01-25`` (:data:`FINAL_2017_COLUMN_DOCS`). The primary release's catalog is
:mod:`rl_pipeline.catalog_2016`.

2017 meanings are short paraphrases of the official TravisTorrent data-format page
(``TestRoots/travistorrent-site`` ``page_dataformat.md``, last changed in commit
``d3f5ece6d73dcc440c13a3668d5038fbdca2f703`` on 2017-02-09; file sha256
``cd7cc220d67b0df3f1ef08d847195335e48157bd78d0cb8bccdc0280de1a8801`` when read on 2026-10-13).
Five columns of the 2017 dump are not on that page; their entries say so, and the dictionary
builder adds *measured* evidence instead of guessing.

Availability tags (ADR-0005/0006 leakage rules):

* ``post_build`` — only known after the build ran (label, duration, all build-log analysis).
* ``uncertain`` — the docs do not bound the value to the build start, so it may contain future
  information. **Excluded from model inputs by default.**
* ``pre_build`` — known when the build is triggered.

Roles: ``identifier`` / ``context`` (never a model feature), ``feature_candidate``, ``label``,
``outcome`` (post-build; usable only lagged, for builds that had already finished), ``excluded``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rl_pipeline.schema import FINAL_2017_NAMES

Availability = Literal["pre_build", "post_build", "uncertain"]
Role = Literal["identifier", "context", "feature_candidate", "label", "outcome", "excluded"]
Grain = Literal["build", "job"]


@dataclass(frozen=True, slots=True)
class DataformatDoc:
    """The documentation page a catalog paraphrases (pinned by commit and content hash)."""

    url: str
    commit: str
    sha256: str
    note: str = ""


FINAL_2017_DATAFORMAT = DataformatDoc(
    url="https://raw.githubusercontent.com/TestRoots/travistorrent-site/master/page_dataformat.md",
    commit="d3f5ece6d73dcc440c13a3668d5038fbdca2f703",
    sha256="cd7cc220d67b0df3f1ef08d847195335e48157bd78d0cb8bccdc0280de1a8801",
)


@dataclass(frozen=True, slots=True)
class ColumnDoc:
    name: str
    meaning: str
    documented: bool
    grain: Grain
    role: Role
    availability: Availability
    justification: str


_IDENT = "Identifier — not a model feature."
_CTX = "Context — not a model feature."
_LOG = "Produced by analysing the finished build's log; forbidden for the current build."
_DIFF = "Computed from the commits being built, which exist before the build starts."


def _c(
    name: str,
    meaning: str,
    role: Role,
    availability: Availability,
    justification: str,
    *,
    grain: Grain = "build",
    documented: bool = True,
) -> ColumnDoc:
    return ColumnDoc(name, meaning, documented, grain, role, availability, justification)


FINAL_2017_COLUMN_DOCS: dict[str, ColumnDoc] = {
    d.name: d
    for d in (
        _c(
            "tr_build_id",
            "Travis CI build id of the analysed build.",
            "identifier",
            "pre_build",
            f"Assigned when the build is created. {_IDENT}",
        ),
        _c(
            "gh_project_name",
            "GitHub repository name (owner/repo).",
            "identifier",
            "pre_build",
            f"Known before the build. {_IDENT}",
        ),
        _c(
            "gh_is_pr",
            "Whether a pull request triggered the build.",
            "feature_candidate",
            "pre_build",
            "Property of the trigger event.",
        ),
        _c(
            "gh_pr_created_at",
            "For PR builds: when the pull request was opened (UTC).",
            "feature_candidate",
            "pre_build",
            "A PR build needs an open PR, so the PR predates it. Measured exceptions (PR created "
            "after the build started; data errors) are counted under Invariant checks — features "
            "derived from it (e.g. PR age) must tolerate negative values.",
        ),
        _c(
            "gh_pull_req_num",
            "For PR builds: the pull request number on GitHub.",
            "identifier",
            "pre_build",
            f"Known at trigger time. {_IDENT}",
        ),
        _c(
            "gh_lang",
            "Dominant repository language according to GitHub (partition key).",
            "context",
            "pre_build",
            f"Repository metadata, used to select the Java subset. {_CTX}",
        ),
        _c(
            "git_merged_with",
            "For commits on a PR: how that PR was eventually closed (merge button, manual "
            "merge, ...).",
            "excluded",
            "uncertain",
            "Describes how the PR was closed, which can happen after the build — possible future "
            "information.",
        ),
        _c(
            "git_branch",
            "Branch that was built.",
            "context",
            "pre_build",
            f"Known at trigger time. {_CTX}",
        ),
        _c(
            "gh_num_commits_in_push",
            "Number of commits in the push that triggered the build (from GHTorrent push "
            "events; nil when no push event was recorded).",
            "feature_candidate",
            "pre_build",
            "The triggering push precedes the build.",
        ),
        _c(
            "gh_commits_in_push",
            "The commits contained in the triggering push.",
            "identifier",
            "pre_build",
            f"The triggering push precedes the build. {_IDENT}",
        ),
        _c(
            "git_prev_commit_resolution_status",
            "Why the backwards walk for the previously built commit stopped: no previous "
            "build, previous build found, or a merge point reached.",
            "feature_candidate",
            "pre_build",
            "Derived from git history up to the built commit.",
        ),
        _c(
            "git_prev_built_commit",
            "Commit of the previous build on the linearised history (nil at a merge point).",
            "identifier",
            "pre_build",
            f"Refers to an earlier commit. {_IDENT}",
        ),
        _c(
            "tr_prev_build",
            "Build triggered by git_prev_built_commit (nil at a merge point).",
            "identifier",
            "pre_build",
            "The id is known at trigger time, but that build's outcome may not be (use the "
            f"finished-before rule). {_IDENT}",
        ),
        _c(
            "gh_first_commit_created_at",
            "Timestamp of the first commit in the triggering push (UTC; nil when no push "
            "event was recorded).",
            "feature_candidate",
            "pre_build",
            "The triggering push precedes the build.",
        ),
        _c(
            "gh_team_size",
            "Developers who committed directly or merged PRs in the 3 months up to the build "
            "trigger.",
            "feature_candidate",
            "pre_build",
            "The docs bound it to the window ending when the build was triggered.",
        ),
        _c(
            "git_all_built_commits",
            "All commits built by this build, walking back to (excluding) the previous built "
            "commit or up to a merge point; the basis of the git_diff_* / gh_diff_* columns.",
            "identifier",
            "pre_build",
            f"List of commit SHAs that exist before the build. {_IDENT}",
        ),
        _c(
            "git_num_all_built_commits",
            "Number of commits in git_all_built_commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "git_trigger_commit",
            "Commit that triggered the build.",
            "identifier",
            "pre_build",
            f"Known at trigger time. {_IDENT}",
        ),
        _c(
            "tr_virtual_merged_into",
            "For PR builds: the target-branch commit the PR commit was merged into for testing.",
            "identifier",
            "pre_build",
            f"Known at trigger time. {_IDENT}",
        ),
        _c(
            "gh_num_issue_comments",
            "For commits linked to a PR: number of discussion comments on that PR.",
            "excluded",
            "uncertain",
            "Not time-bounded to the build start in the docs; may count comments written after "
            "the build — possible future information.",
        ),
        _c(
            "gh_num_commit_comments",
            "Number of GitHub comments on the built commits.",
            "excluded",
            "uncertain",
            "Not time-bounded to the build start in the docs; may count comments written after "
            "the build — possible future information.",
        ),
        _c(
            "gh_num_pr_comments",
            "For PR builds: number of code-review comments on the pull request.",
            "excluded",
            "uncertain",
            "Not time-bounded to the build start in the docs; may count review comments written "
            "after the build — possible future information.",
        ),
        _c(
            "git_diff_src_churn",
            "Lines of production code changed across the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "git_diff_test_churn",
            "Lines of test code changed across the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_files_added",
            "Files added by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_files_deleted",
            "Files deleted by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_files_modified",
            "Files modified by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_tests_added",
            "Test cases added by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_tests_deleted",
            "Test cases deleted by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_src_files",
            "Production source files changed by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_doc_files",
            "Documentation files changed by the built commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_diff_other_files",
            "Changed files that are neither source code nor documentation.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_num_commits_on_files_touched",
            "Distinct commits in the 3 months up to the trigger that touched the files changed "
            "by the built commits (how active that code area is).",
            "feature_candidate",
            "pre_build",
            "The docs bound it to the window ending when the build was triggered.",
        ),
        _c(
            "gh_sloc",
            "Executable production source lines in the whole repository (comments excluded).",
            "feature_candidate",
            "pre_build",
            "Repository state at the built commit.",
        ),
        _c(
            "gh_test_lines_per_kloc",
            "Test density: lines of test code per 1,000 gh_sloc.",
            "feature_candidate",
            "pre_build",
            "Repository state at the built commit.",
        ),
        _c(
            "gh_test_cases_per_kloc",
            "Test density: test cases per 1,000 gh_sloc.",
            "feature_candidate",
            "pre_build",
            "Repository state at the built commit.",
        ),
        _c(
            "gh_asserts_cases_per_kloc",
            "Assert density: assertions per 1,000 gh_sloc.",
            "feature_candidate",
            "pre_build",
            "Repository state at the built commit.",
        ),
        _c(
            "gh_by_core_team_member",
            "Whether the commit author committed or merged at least once in the 3 months "
            "before the commit (core team member).",
            "feature_candidate",
            "pre_build",
            "The docs bound it to the 3 months before the commit.",
        ),
        _c(
            "gh_description_complexity",
            "For PR builds: total words in the PR title and description.",
            "feature_candidate",
            "pre_build",
            "The PR exists before its builds. Caveat: the docs do not say the text was captured "
            "at build time, so later edits cannot be ruled out.",
        ),
        _c(
            "gh_pushed_at",
            "Timestamp of the triggering push as reported by GitHub (UTC).",
            "feature_candidate",
            "pre_build",
            "The push precedes the build.",
        ),
        _c(
            "gh_build_started_at",
            "Build trigger/start timestamp as reported by Travis (UTC); the docs describe it as "
            "the Travis-provided time of the triggering push.",
            "feature_candidate",
            "pre_build",
            "Trigger time; used for ordering, history windows and time features. UTC confirmed "
            "in M2 review: equal to the secondary archive's explicit-UTC started_at for all "
            "27,055 builds joined on tr_build_id. Caveat: a restart overwrites it "
            "(DATASET_AUDIT §4.3).",
        ),
        _c(
            "gh_repo_age",
            "Not described on the official data-format page.",
            "feature_candidate",
            "pre_build",
            "Measured to advance ~1 day per day of gh_build_started_at within a project (see "
            "evidence), i.e. repository age at build time.",
            documented=False,
        ),
        _c(
            "gh_repo_num_commits",
            "Not described on the official data-format page.",
            "feature_candidate",
            "pre_build",
            "Measured to vary over time within a project (see evidence), consistent with a "
            "commit count at build time rather than a crawl-time snapshot.",
            documented=False,
        ),
        _c(
            "tr_job_id",
            "Travis job id of the analysed job (one CSV row per job).",
            "identifier",
            "pre_build",
            f"Assigned when the build's jobs are created. {_IDENT}",
            grain="job",
        ),
        _c(
            "tr_build_number",
            "Per-project serial build number.",
            "identifier",
            "pre_build",
            f"Assigned when the build is created. {_IDENT}",
        ),
        _c(
            "tr_log_lan",
            "Primary programming language detected by build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_status",
            "Overall return status detected by build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_setup_time",
            "Seconds of setup before the script phase, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_analyzer",
            "Which build-log analyzer processed the log.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_frameworks",
            "Test frameworks detected by build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_bool_tests_ran",
            "Whether tests ran, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_bool_tests_failed",
            "Whether tests failed, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_num_tests_ok",
            "Number of passing tests, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_num_tests_failed",
            "Number of failing tests, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_num_tests_run",
            "Total number of tests run, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_num_tests_skipped",
            "Number of skipped tests, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_num_test_suites_run",
            "Not described on the official data-format page (the name suggests test suites run, "
            "from build-log analysis; unverified).",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
            documented=False,
        ),
        _c(
            "tr_log_num_test_suites_ok",
            "Not described on the official data-format page (the name suggests passing test "
            "suites, from build-log analysis; unverified).",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
            documented=False,
        ),
        _c(
            "tr_log_num_test_suites_failed",
            "Not described on the official data-format page (the name suggests failing test "
            "suites, from build-log analysis; unverified).",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
            documented=False,
        ),
        _c(
            "tr_log_tests_failed",
            "Names of the failing tests, from build-log analysis (free text).",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_testduration",
            "Seconds spent running tests, from build-log analysis.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_log_buildduration",
            "Seconds spent in the build tool (e.g. Maven, Ant), from build-log analysis; "
            "includes the test phase.",
            "outcome",
            "post_build",
            _LOG,
            grain="job",
        ),
        _c(
            "tr_original_commit",
            "Commit SHA Travis built; for PRs possibly a virtual merge commit not in the "
            "repository.",
            "identifier",
            "pre_build",
            f"Known at trigger time. {_IDENT}",
        ),
        _c(
            "tr_duration",
            "Full build duration reported by the Travis API. Summed job time, not wall-clock "
            "(DATASET_AUDIT §4.1; equal to that archive's duration for 26,990 of 27,055 builds "
            "joined in M2 review), so started_at + tr_duration only bounds the finish time.",
            "outcome",
            "post_build",
            "Only known once the build finished; usable only as lagged history of finished builds.",
        ),
        _c(
            "tr_status",
            "Build status from the Travis API (passed, failed, errored, canceled, ...).",
            "label",
            "post_build",
            "The prediction target (ADR-0005); only known once the build finished.",
        ),
        _c(
            "tr_jobs",
            "Travis job ids of the build, '#'-separated.",
            "identifier",
            "pre_build",
            f"Jobs are created together with the build. {_IDENT}",
        ),
    )
}

assert tuple(FINAL_2017_COLUMN_DOCS) == FINAL_2017_NAMES, "docs must follow file order exactly"

# Spec (2016-style) names -> real 2017 column names. Each target is checked against the real
# header by the dictionary builder; renamed ones moved to the git_diff_* / gh_diff_* families.
FINAL_2017_SPEC_NAME_MAP: dict[str, str] = {
    "gh_src_churn": "git_diff_src_churn",
    "gh_test_churn": "git_diff_test_churn",
    "gh_files_added": "gh_diff_files_added",
    "gh_files_deleted": "gh_diff_files_deleted",
    "gh_files_modified": "gh_diff_files_modified",
    "gh_tests_added": "gh_diff_tests_added",
    "gh_tests_deleted": "gh_diff_tests_deleted",
    "gh_src_files": "gh_diff_src_files",
    "gh_doc_files": "gh_diff_doc_files",
    "gh_other_files": "gh_diff_other_files",
    "gh_commits_on_files_touched": "gh_num_commits_on_files_touched",
    "gh_sloc": "gh_sloc",
    "gh_test_lines_per_kloc": "gh_test_lines_per_kloc",
    "gh_test_cases_per_kloc": "gh_test_cases_per_kloc",
    "gh_asserts_cases_per_kloc": "gh_asserts_cases_per_kloc",
    "gh_team_size": "gh_team_size",
    "gh_by_core_team_member": "gh_by_core_team_member",
    "gh_num_commits_in_push": "gh_num_commits_in_push",
    "gh_description_complexity": "gh_description_complexity",
}


@dataclass(frozen=True, slots=True)
class BuildInputMapping:
    """How one ``rl_core.features.change.BUILD_INPUT_FIELDS`` input maps onto a raw column."""

    field: str
    column: str | None  # None: no column of this release provides it
    status: Literal["direct", "approximate", "none"]
    note: str = ""


# rl_core build inputs -> 2017 columns (secondary dataset; never a training source, ADR-0017).
FINAL_2017_BUILD_INPUT_MAP: tuple[BuildInputMapping, ...] = (
    BuildInputMapping("src_churn", "git_diff_src_churn", "direct", "broken in this release"),
    BuildInputMapping("test_churn", "git_diff_test_churn", "direct", "broken in this release"),
    BuildInputMapping("files_added", "gh_diff_files_added", "direct"),
    BuildInputMapping("files_deleted", "gh_diff_files_deleted", "direct"),
    BuildInputMapping("files_modified", "gh_diff_files_modified", "direct"),
    BuildInputMapping("tests_added", "gh_diff_tests_added", "direct"),
    BuildInputMapping("tests_deleted", "gh_diff_tests_deleted", "direct"),
    BuildInputMapping("src_files", "gh_diff_src_files", "direct", "broken in this release"),
    BuildInputMapping("doc_files", "gh_diff_doc_files", "direct", "broken in this release"),
    BuildInputMapping("other_files", "gh_diff_other_files", "direct"),
    BuildInputMapping("sloc", "gh_sloc", "direct"),
    BuildInputMapping("test_lines_per_kloc", "gh_test_lines_per_kloc", "direct"),
    BuildInputMapping("test_cases_per_kloc", "gh_test_cases_per_kloc", "direct"),
    BuildInputMapping("asserts_per_kloc", "gh_asserts_cases_per_kloc", "direct"),
    BuildInputMapping("num_commits_in_push", "gh_num_commits_in_push", "direct", "all NULL"),
    BuildInputMapping("num_all_built_commits", "git_num_all_built_commits", "direct"),
    BuildInputMapping("team_size", "gh_team_size", "direct"),
    BuildInputMapping("by_core_team_member", "gh_by_core_team_member", "direct"),
    BuildInputMapping("is_pr", "gh_is_pr", "direct"),
    BuildInputMapping("description_complexity", "gh_description_complexity", "direct"),
    BuildInputMapping("commits_on_files_touched", "gh_num_commits_on_files_touched", "direct"),
    BuildInputMapping("repo_age_days", "gh_repo_age", "direct"),
)
