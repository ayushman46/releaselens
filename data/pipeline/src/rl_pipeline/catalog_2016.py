"""Column knowledge for the **primary** release ``travistorrent_5_3_2016.csv.gz`` (ADR-0017).

Meanings paraphrase the TravisTorrent data-format page as it stood on 2016-11-09
(``TestRoots/travistorrent-site`` ``page_dataformat.md`` at commit
``7466b3d2ca08f1f4b792007705ae5bafb33a8224``, file sha256
``863c28af7b11a19025b4c622ddc7c153945adaad159316e731eb78069237444d``, read 2026-10-13), the last
version that describes this 56-column format; in December 2016 the page switched to the 2017
format. The page's first version (commit ``1ca2ce08d42e69f9f42a985381184c0d936b3b92``,
2016-07-28, sha256 ``3ab6510ad2513a3f2e36fe2fa768688e3d81f5a4bcc819054bb5c86a3207720f``) defines
several columns differently (repository-wide instead of per-build file counts, "test cases" instead
of "lines of test code", no time window for team size and file activity, "project start" for
``gh_first_commit_created_at``). Both versions post-date the dump (dated 2016-03-05 by its file
name), so neither is authoritative. Where they disagree the entry says so, and the dictionary
builder adds measured evidence; where the data contradicts both (``gh_first_commit_created_at``),
the measured behaviour wins.

Tags follow :mod:`rl_pipeline.catalog` and the M2b rules: ``post_build`` = label, duration, every
log-derived ``tr_*`` column and ``tr_ci_latency``; ``uncertain`` = ``git_merged_with``, the three
comment counts, ``gh_description_complexity`` (final PR text) and ``tr_lan`` (log-derived language,
never a feature); everything else is ``pre_build``.
"""

from __future__ import annotations

from rl_pipeline.catalog import (
    Availability,
    BuildInputMapping,
    ColumnDoc,
    DataformatDoc,
    Grain,
    Role,
)
from rl_pipeline.schema import TT_5_3_2016_NAMES

TT_5_3_2016_DATAFORMAT = DataformatDoc(
    url="https://raw.githubusercontent.com/TestRoots/travistorrent-site/"
    "7466b3d2ca08f1f4b792007705ae5bafb33a8224/page_dataformat.md",
    commit="7466b3d2ca08f1f4b792007705ae5bafb33a8224",
    sha256="863c28af7b11a19025b4c622ddc7c153945adaad159316e731eb78069237444d",
    note=(
        "Last page version describing this 56-column format (2016-11-09). The first version "
        "(commit 1ca2ce08d42e, 2016-07-28) defines gh_first_commit_created_at, gh_team_size, "
        "git_commits, the comment counts, the change columns, gh_tests_added/deleted, "
        "gh_src/doc/other_files, gh_commits_on_files_touched and gh_description_complexity "
        "differently. Both post-date the dump (dated 2016-03-05 by its file name); measured "
        "evidence below decides where possible."
    ),
)

_IDENT = "Identifier — not a model feature."
_CTX = "Context — not a model feature."
_LOG = "Produced by analysing the finished build's log; forbidden for the current build."
_DIFF = "Computed from the commits being built, which exist before the build starts."
_DOC_A = "the 2016-07-28 page version"


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


def _log(name: str, meaning: str) -> ColumnDoc:
    return _c(name, meaning, "outcome", "post_build", _LOG, grain="job")


TT_5_3_2016_COLUMN_DOCS: dict[str, ColumnDoc] = {
    d.name: d
    for d in (
        _c(
            "row",
            "Row number of the job row in the dump; documented as the unique identifier of a "
            "build job.",
            "identifier",
            "pre_build",
            "Dump artifact (measured unique), used as the conversion's unique key and last sort "
            f"column because tr_job_id is not unique in this release. {_IDENT}",
            grain="job",
        ),
        _c(
            "git_commit",
            "SHA-1 of the commit that triggered the build.",
            "identifier",
            "pre_build",
            f"Known at trigger time. {_IDENT}",
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
            "Whether the build was triggered as part of a pull request.",
            "feature_candidate",
            "pre_build",
            "Property of the trigger event.",
        ),
        _c(
            "git_merged_with",
            "For PR builds: how the pull request was merged. The page calls it the SHA-1 of the "
            "merging commit, but the values are a few categorical codes (see Distinct).",
            "excluded",
            "uncertain",
            "Describes how the PR was eventually merged, which can happen after the build — "
            "possible future information.",
        ),
        _c(
            "gh_lang",
            "Dominant repository language according to GitHub (partition key).",
            "context",
            "pre_build",
            f"Repository metadata, used to select the Java subset. {_CTX}",
        ),
        _c(
            "git_branch",
            "Branch git_commit was committed on.",
            "context",
            "pre_build",
            f"Known at trigger time. {_CTX}",
        ),
        _c(
            "gh_first_commit_created_at",
            "Documented as the timestamp of the first commit in the triggering push "
            f"({_DOC_A}: first commit of the whole repository). Measured: a copy of "
            "tr_started_at in UK local time (GMT/BST), not a commit timestamp (see evidence).",
            "excluded",
            "pre_build",
            "Carries no information beyond tr_started_at and is not UTC (measured, see "
            "evidence): never use it as a commit time or a feature.",
        ),
        _c(
            "gh_team_size",
            "Developers who committed directly or merged PRs in the 3 months up to the build "
            f"trigger ({_DOC_A}: size of the team, no window).",
            "feature_candidate",
            "pre_build",
            "The later page bounds it to the window ending at the trigger; the earlier page does "
            "not state a window.",
        ),
        _c(
            "git_commits",
            "Commits of the triggering push minus the built commit, '#'-separated "
            f"({_DOC_A}: directly preceding commits without their own build).",
            "identifier",
            "pre_build",
            f"List of commit SHAs that exist before the build. {_IDENT}",
        ),
        _c(
            "git_num_commits",
            "Number of entries in git_commits.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_num_issue_comments",
            "If git_commit is linked to a PR: number of discussion comments on that PR.",
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
            "gh_src_churn",
            "Lines of production code changed by the commits built by this build "
            f"({_DOC_A}: churn of git_commit only).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_test_churn",
            "Lines of test code changed by the commits built by this build "
            f"({_DOC_A}: churn of git_commit only).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_files_added",
            "Files added by the commits built by this build.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_files_deleted",
            "Files deleted by the commits built by this build.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_files_modified",
            "Files modified by the commits built by this build.",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_tests_added",
            "Tests added by the built commits: lines of test code per the later page, test cases "
            f"(e.g. @Test annotations) per {_DOC_A} (see evidence).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_tests_deleted",
            "Tests deleted by the built commits (same two definitions as gh_tests_added).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_src_files",
            "Production source files changed by the built commits "
            f"({_DOC_A}: production files in the whole repository; see evidence).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_doc_files",
            "Documentation files changed by the built commits "
            f"({_DOC_A}: documentation files in the repository).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_other_files",
            "Changed files that are neither production code nor documentation "
            f"({_DOC_A}: such files in the repository).",
            "feature_candidate",
            "pre_build",
            _DIFF,
        ),
        _c(
            "gh_commits_on_files_touched",
            "Distinct commits in the 3 months up to the trigger that touched the files changed by "
            f"the built commits ({_DOC_A}: all earlier commits on those files, no window).",
            "feature_candidate",
            "pre_build",
            "Counts commits that precede the trigger under either page version.",
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
            "Whether the built commit was authored by a core team member.",
            "feature_candidate",
            "pre_build",
            "Property of the commit author before the build.",
        ),
        _c(
            "gh_description_complexity",
            "For PR builds: total words in the pull request title and description.",
            "excluded",
            "uncertain",
            "Taken from the final PR text, which can be edited after the build (ADR-0017 "
            "context) — possible future information.",
        ),
        _c(
            "tr_build_id",
            "Travis CI build id.",
            "identifier",
            "pre_build",
            f"Assigned when the build is created. {_IDENT}",
        ),
        _c(
            "gh_pull_req_num",
            "For PR builds: the pull request number on GitHub.",
            "identifier",
            "pre_build",
            f"Known at trigger time. {_IDENT}",
        ),
        _c(
            "tr_status",
            "Build status from the Travis API (passed, failed, errored, canceled, started).",
            "label",
            "post_build",
            "The prediction target (ADR-0005); only known once the build finished.",
        ),
        _c(
            "tr_duration",
            "Overall build duration reported by Travis, in seconds (summed job time per "
            "DATASET_AUDIT §4.1, so started_at + duration only bounds the finish time).",
            "outcome",
            "post_build",
            "Only known once the build finished; usable only as lagged history of finished builds.",
        ),
        _c(
            "tr_started_at",
            "Start of the build process (UTC wall-clock, verified against the secondary "
            "archive; see evidence).",
            "feature_candidate",
            "pre_build",
            "Trigger time; used for ordering, history windows and time features. Caveat: a "
            "restart overwrites it (DATASET_AUDIT §4.3).",
        ),
        _c(
            "tr_jobs",
            "Travis job ids of the build, as a bracketed comma-separated list ('[id, id, ...]').",
            "identifier",
            "pre_build",
            f"Jobs are created together with the build (the matrix is known at creation). {_IDENT}",
        ),
        _c(
            "tr_build_number",
            "Per-project serial build number on Travis.",
            "identifier",
            "pre_build",
            f"Assigned when the build is created. {_IDENT}",
        ),
        _c(
            "tr_job_id",
            "Travis job id of this row; documented as one of tr_jobs (not always true, see "
            "invariants).",
            "identifier",
            "pre_build",
            f"Assigned when the build's jobs are created. {_IDENT}",
            grain="job",
        ),
        _c(
            "tr_lan",
            "Language of the build as recognised by the build-log analyzer.",
            "identifier",
            "uncertain",
            "Derived from the build log (formally post-build) while the language itself is a "
            "repository property already given by gh_lang; tagged uncertain. Identifier — not a "
            "model feature.",
            grain="job",
        ),
        _log("tr_setup_time", "Setup time before the Travis build started, in seconds."),
        _log(
            "tr_analyzer",
            "Build-log analyzer that processed the log (e.g. java-ant, java-maven, java-gradle).",
        ),
        _log("tr_frameworks", "Test frameworks the analyzer recognised, '#'-separated."),
        _log("tr_tests_ok", "Number of passed tests, if the analyzer could tell."),
        _log("tr_tests_fail", "Number of failed tests, if the analyzer could tell."),
        _log("tr_tests_run", "Number of tests run, if the analyzer could tell."),
        _log("tr_tests_skipped", "Number of skipped or ignored tests, if the analyzer could tell."),
        _log("tr_failed_tests", "Names of the failed tests, '#'-separated (free text)."),
        _log("tr_testduration", "Seconds spent running the tests."),
        _log(
            "tr_purebuildduration",
            "Seconds the build itself ran, without Travis scheduling and provisioning.",
        ),
        _log("tr_tests_ran", "Whether tests ran in this job."),
        _log("tr_tests_failed", "Whether tests failed in this job."),
        _c(
            "git_num_committers",
            "Documented as the number of people who committed to the project; measured to count "
            "the committers of the built commits (see evidence).",
            "feature_candidate",
            "pre_build",
            "Measured to be bounded by git_num_commits + 1 (evidence), i.e. committers of commits "
            "that exist before the build; the page's project-wide wording is not time-bounded, "
            "so verify before relying on it.",
        ),
        _c(
            "tr_num_jobs",
            "Number of jobs of the build (length of tr_jobs).",
            "feature_candidate",
            "pre_build",
            "The build matrix is known when the build is created.",
        ),
        _c(
            "tr_prev_build",
            "Link to the previous build, given as its tr_build_id; -1 (also written -1.0) when "
            "there is none. Measured to differ between jobs of the same build.",
            "identifier",
            "pre_build",
            "Linkage to an earlier build: its id is known at trigger time, but that build's "
            "outcome may not be (use the finished-before rule). -1 is a placeholder that staging "
            f"turns into NULL. {_IDENT}",
            grain="job",
        ),
        _c(
            "tr_ci_latency",
            "Latency induced by Travis (scheduling, build pick-up), in seconds; -1 placeholder.",
            "outcome",
            "post_build",
            "Only observable once Travis picked the build up, i.e. after the trigger moment.",
        ),
    )
}

assert tuple(TT_5_3_2016_COLUMN_DOCS) == TT_5_3_2016_NAMES, "docs must follow file order exactly"

# The spec's change/test/codebase/process names are this release's own names (ADR-0017).
TT_5_3_2016_SPEC_NAME_MAP: dict[str, str] = {
    "gh_src_churn": "gh_src_churn",
    "gh_test_churn": "gh_test_churn",
    "gh_files_added": "gh_files_added",
    "gh_files_deleted": "gh_files_deleted",
    "gh_files_modified": "gh_files_modified",
    "gh_tests_added": "gh_tests_added",
    "gh_tests_deleted": "gh_tests_deleted",
    "gh_src_files": "gh_src_files",
    "gh_doc_files": "gh_doc_files",
    "gh_other_files": "gh_other_files",
    "gh_commits_on_files_touched": "gh_commits_on_files_touched",
    "gh_sloc": "gh_sloc",
    "gh_test_lines_per_kloc": "gh_test_lines_per_kloc",
    "gh_test_cases_per_kloc": "gh_test_cases_per_kloc",
    "gh_asserts_cases_per_kloc": "gh_asserts_cases_per_kloc",
    "gh_team_size": "gh_team_size",
    "gh_by_core_team_member": "gh_by_core_team_member",
    "gh_description_complexity": "gh_description_complexity",
    "tr_started_at": "tr_started_at",
    "tr_prev_build": "tr_prev_build",
}

# rl_core build inputs (rl_core.features.change.BUILD_INPUT_FIELDS) -> 2016 columns. The staging
# / mart layer (M3) applies this mapping; the dictionary prints it next to measured profiles.
TT_5_3_2016_BUILD_INPUT_MAP: tuple[BuildInputMapping, ...] = (
    BuildInputMapping("src_churn", "gh_src_churn", "direct"),
    BuildInputMapping("test_churn", "gh_test_churn", "direct"),
    BuildInputMapping("files_added", "gh_files_added", "direct"),
    BuildInputMapping("files_deleted", "gh_files_deleted", "direct"),
    BuildInputMapping("files_modified", "gh_files_modified", "direct"),
    BuildInputMapping(
        "tests_added", "gh_tests_added", "approximate", "page versions disagree: lines vs cases"
    ),
    BuildInputMapping(
        "tests_deleted", "gh_tests_deleted", "approximate", "page versions disagree: lines vs cases"
    ),
    BuildInputMapping("src_files", "gh_src_files", "direct"),
    BuildInputMapping("doc_files", "gh_doc_files", "direct"),
    BuildInputMapping("other_files", "gh_other_files", "direct"),
    BuildInputMapping("sloc", "gh_sloc", "direct"),
    BuildInputMapping("test_lines_per_kloc", "gh_test_lines_per_kloc", "direct"),
    BuildInputMapping("test_cases_per_kloc", "gh_test_cases_per_kloc", "direct"),
    BuildInputMapping("asserts_per_kloc", "gh_asserts_cases_per_kloc", "direct", "renamed"),
    BuildInputMapping(
        "num_commits_in_push",
        None,
        "none",
        "no push-level count (GHTorrent push events); excluded per ADR-0004",
    ),
    BuildInputMapping(
        "num_all_built_commits",
        "git_num_commits",
        "approximate",
        "entries of git_commits, which excludes the built commit (see evidence)",
    ),
    BuildInputMapping("team_size", "gh_team_size", "direct"),
    BuildInputMapping("by_core_team_member", "gh_by_core_team_member", "direct"),
    BuildInputMapping("is_pr", "gh_is_pr", "direct"),
    BuildInputMapping(
        "description_complexity",
        "gh_description_complexity",
        "direct",
        "uncertain: excluded from model inputs by default",
    ),
    BuildInputMapping("commits_on_files_touched", "gh_commits_on_files_touched", "direct"),
    BuildInputMapping(
        "repo_age_days",
        None,
        "none",
        "no repository-age column; gh_first_commit_created_at is a copy of tr_started_at",
    ),
)
