"""Feature schema (spec §8.2) and leakage rules (spec §8.3).

Changing a feature's definition or the list requires bumping ``FEATURE_SCHEMA_VERSION``; the
version is stored with every training run and every live prediction.
"""

from __future__ import annotations

from typing import Final

FEATURE_SCHEMA_VERSION: Final = "1.0.0"

CHANGE_FEATURES: Final = (
    "src_churn",
    "test_churn",
    "total_churn",
    "log1p_src_churn",
    "log1p_test_churn",
    "log1p_total_churn",
    "files_added",
    "files_deleted",
    "files_modified",
    "files_touched",
    "src_files",
    "doc_files",
    "other_files",
)
TEST_FEATURES: Final = ("tests_added", "tests_deleted", "test_churn_ratio", "touches_tests")
CODEBASE_FEATURES: Final = (
    "log1p_sloc",
    "test_lines_per_kloc",
    "test_cases_per_kloc",
    "asserts_per_kloc",
)
PROCESS_FEATURES: Final = (
    "num_commits_in_push",
    "num_all_built_commits",
    "team_size",
    "by_core_team_member",
    "is_pr",
    "description_complexity",
    "commits_on_files_touched",
)
HISTORY_FEATURES: Final = (
    "prev_build_failed",
    "fail_rate_last_5",
    "fail_rate_last_20",
    "builds_since_last_failure",
    "hours_since_last_build",
    "prev_build_duration",
    "prev_duration_zscore_vs_project_last_20",
    "history_size",
    "project_age_days",
)
TIME_FEATURES: Final = ("hour_of_week_sin", "hour_of_week_cos")

FEATURE_NAMES: Final = (
    CHANGE_FEATURES + TEST_FEATURES + CODEBASE_FEATURES + PROCESS_FEATURES + HISTORY_FEATURES
)
"""Default model inputs. TIME_FEATURES are opt-in: used only if they improve validation PR-AUC."""

ALL_FEATURE_NAMES: Final = FEATURE_NAMES + TIME_FEATURES

# Inputs that must never reach the model for the *current* build: they are (or leak) the label,
# are observed after the build starts, or are not time-bounded to the build start (spec §8.3,
# docs/data/DATA_DICTIONARY.md "uncertain" columns).
FORBIDDEN_INPUTS: Final = frozenset(
    {
        "tr_status",
        "status",
        "label",
        "tr_duration",
        "duration",
        "duration_s",
        "finished_at",
        "tr_log_lan",
        "tr_log_status",
        "tr_log_setup_time",
        "tr_log_analyzer",
        "tr_log_frameworks",
        "tr_log_bool_tests_ran",
        "tr_log_bool_tests_failed",
        "tr_log_num_tests_ok",
        "tr_log_num_tests_failed",
        "tr_log_num_tests_run",
        "tr_log_num_tests_skipped",
        "tr_log_num_test_suites_run",
        "tr_log_num_test_suites_ok",
        "tr_log_num_test_suites_failed",
        "tr_log_tests_failed",
        "tr_log_testduration",
        "tr_log_buildduration",
        "git_merged_with",
        "gh_num_issue_comments",
        "gh_num_commit_comments",
        "gh_num_pr_comments",
        # 2016-release (ADR-0017) post-build / log-derived columns
        "tr_lan",
        "tr_setup_time",
        "tr_analyzer",
        "tr_frameworks",
        "tr_tests_ok",
        "tr_tests_fail",
        "tr_tests_run",
        "tr_tests_skipped",
        "tr_failed_tests",
        "tr_testduration",
        "tr_purebuildduration",
        "tr_tests_ran",
        "tr_tests_failed",
        "tr_ci_latency",
        # 2016-release columns excluded as unreliable (docs/data/DATA_DICTIONARY.md)
        "gh_description_complexity",
        "gh_first_commit_created_at",
        # post-deployment and incident information
        "regression_status",
        "severity_score",
        "rollback",
        "incident",
    }
)

HUMAN_LABELS: Final[dict[str, str]] = {
    "src_churn": "Source lines changed",
    "test_churn": "Test lines changed",
    "total_churn": "Total lines changed",
    "log1p_src_churn": "Source lines changed",
    "log1p_test_churn": "Test lines changed",
    "log1p_total_churn": "Total lines changed",
    "files_added": "Files added",
    "files_deleted": "Files deleted",
    "files_modified": "Files modified",
    "files_touched": "Files touched",
    "src_files": "Source files touched",
    "doc_files": "Documentation files touched",
    "other_files": "Other files touched",
    "tests_added": "Test cases added",
    "tests_deleted": "Test cases deleted",
    "test_churn_ratio": "Share of the change that is test code",
    "touches_tests": "Change touches tests",
    "log1p_sloc": "Codebase size (lines of code)",
    "test_lines_per_kloc": "Test lines per 1,000 lines of code",
    "test_cases_per_kloc": "Test cases per 1,000 lines of code",
    "asserts_per_kloc": "Assertions per 1,000 lines of code",
    "num_commits_in_push": "Commits in the push",
    "num_all_built_commits": "Commits since the previous built commit",
    "team_size": "Active team size (last 3 months)",
    "by_core_team_member": "Authored by a core team member",
    "is_pr": "Pull-request build",
    "description_complexity": "PR description length",
    "commits_on_files_touched": "Recent commits on the touched files (file hotness)",
    "prev_build_failed": "Previous finished build failed",
    "fail_rate_last_5": "Failure rate of the last 5 finished builds",
    "fail_rate_last_20": "Failure rate of the last 20 finished builds",
    "builds_since_last_failure": "Builds since the last failure",
    "hours_since_last_build": "Hours since the previous build started",
    "prev_build_duration": "Duration of the previous finished build",
    "prev_duration_zscore_vs_project_last_20": "Previous build duration vs. recent norm",
    "history_size": "Finished builds in the project history",
    "project_age_days": "Repository age (days)",
    "hour_of_week_sin": "Time of week",
    "hour_of_week_cos": "Time of week",
}
