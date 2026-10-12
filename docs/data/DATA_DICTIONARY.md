# Data dictionary — TravisTorrent (Figshare `final-2017-01-25`)

> **Generated** by `python -m rl_pipeline build_data_dictionary` from the converted Parquet of the real dataset. Do not edit by hand; every number below is computed. Machine-readable twin: `docs/data/data_dictionary.json`.

## Source

- File: `final-2017-01-25.csv.gz` · 264,607,174 bytes · md5 `5d359345fecf0bf47be6edb10b5aae58` (= dataset version)
- Figshare DOI [10.6084/m9.figshare.19314170](https://doi.org/10.6084/m9.figshare.19314170) · download URL `https://ndownloader.figshare.com/files/34303124`
- License: **CC BY 4.0**. Attribution: Beller, M., Gousios, G., Zaidman, A. TravisTorrent: Synthesizing Travis CI and GitHub for Full-Stack Research on Continuous Integration. MSR 2017, DOI 10.1109/MSR.2017.24. Data: Figshare DOI 10.6084/m9.figshare.19314170 (CC BY 4.0).
- Column meanings paraphrase the official data-format page (https://raw.githubusercontent.com/TestRoots/travistorrent-site/master/page_dataformat.md, commit `d3f5ece6d73d`). 5 columns are not on that page: `gh_repo_age`, `gh_repo_num_commits`, `tr_log_num_test_suites_run`, `tr_log_num_test_suites_ok`, `tr_log_num_test_suites_failed`.

## Counts

| Scope | Jobs (rows) | Builds | Projects |
|---|---:|---:|---:|
| All languages | 3,881,992 | 925,897 | 948 |
| `gh_lang = go` | 255,358 | 113,940 | 198 |
| `gh_lang = java` | 401,767 | 200,561 | 241 |
| `gh_lang = python` | 1,520,967 | 338,176 | 261 |
| `gh_lang = ruby` | 1,703,900 | 273,220 | 248 |

Java `gh_build_started_at` range (build level, UTC): **2012-01-27 14:36:49 → 2016-08-31 23:59:27**.

### Java label distribution (`tr_status`, one row per build)

| tr_status | Builds | Share |
|---|---:|---:|
| `passed` | 143,137 | 71.37% |
| `failed` | 37,399 | 18.65% |
| `errored` | 19,490 | 9.72% |
| `canceled` | 535 | 0.27% |

Label rule (ADR-0005): `failed`/`errored` → 1, `passed` → 0, `canceled` excluded. Any other status present above is excluded as well (no observed outcome).

## Spec names → real 2017 column names

The spec uses the 2016-style names; the 2017 dump renamed the change-set columns. Every target below was checked against the real header.

| Spec name | Real column | Renamed | In header | Availability |
|---|---|---|---|---|
| `gh_src_churn` | `git_diff_src_churn` | yes | yes | pre_build |
| `gh_test_churn` | `git_diff_test_churn` | yes | yes | pre_build |
| `gh_files_added` | `gh_diff_files_added` | yes | yes | pre_build |
| `gh_files_deleted` | `gh_diff_files_deleted` | yes | yes | pre_build |
| `gh_files_modified` | `gh_diff_files_modified` | yes | yes | pre_build |
| `gh_tests_added` | `gh_diff_tests_added` | yes | yes | pre_build |
| `gh_tests_deleted` | `gh_diff_tests_deleted` | yes | yes | pre_build |
| `gh_src_files` | `gh_diff_src_files` | yes | yes | pre_build |
| `gh_doc_files` | `gh_diff_doc_files` | yes | yes | pre_build |
| `gh_other_files` | `gh_diff_other_files` | yes | yes | pre_build |
| `gh_commits_on_files_touched` | `gh_num_commits_on_files_touched` | yes | yes | pre_build |
| `gh_sloc` | `gh_sloc` | no | yes | pre_build |
| `gh_test_lines_per_kloc` | `gh_test_lines_per_kloc` | no | yes | pre_build |
| `gh_test_cases_per_kloc` | `gh_test_cases_per_kloc` | no | yes | pre_build |
| `gh_asserts_cases_per_kloc` | `gh_asserts_cases_per_kloc` | no | yes | pre_build |
| `gh_team_size` | `gh_team_size` | no | yes | pre_build |
| `gh_by_core_team_member` | `gh_by_core_team_member` | no | yes | pre_build |
| `gh_num_commits_in_push` | `gh_num_commits_in_push` | no | yes | pre_build |
| `gh_description_complexity` | `gh_description_complexity` | no | yes | pre_build |

## Availability summary

- `pre_build` (known when the build is triggered): 43 columns, of which 28 are feature candidates and the rest identifiers/context (never model features).
- `post_build` (label, duration, build-log analysis): 19 columns — forbidden for the current build; usable only as lagged history of builds that had finished.
- `uncertain` (**excluded from model inputs by default**): `git_merged_with`, `gh_num_issue_comments`, `gh_num_commit_comments`, `gh_num_pr_comments`.
- `model_input_default` is true for 24 feature candidates. Feature candidates measured as all-NULL or constant in the Java subset are excluded: `gh_num_commits_in_push`, `gh_first_commit_created_at`, `git_diff_test_churn`, `gh_pushed_at`.

## Measurement

- Subset: `gh_lang = 'java'`. Build-grain columns: one row per tr_build_id (the job row with the lowest tr_job_id); job-grain columns: every job row.
- Null = SQL NULL after conversion (raw empty field or the 'NA' token). Distinct = exact count(DISTINCT), NULL excluded.
- *Varying builds* = Java builds whose jobs disagree on the column (different values, or NULL mixed with a value) — a measured check of the declared grain.
- Stats: min / median / max (and the share of zeros) for numeric feature/outcome columns, min → max for timestamps, TRUE share for booleans. Free-text values are never shown.

## Data-quality flags (Java, measured)

`all_null`: no value at all · `constant`: a single distinct value · `mostly_zero`: ≥ 95% of non-null values are 0. Flagged columns carry little or no signal in this dump; downstream feature selection must take this into account.

| Column | Flags | Null rate | Zero share | Distinct |
|---|---|---:|---:|---:|
| `gh_num_commits_in_push` | all_null | 100.00% | — | 0 |
| `gh_commits_in_push` | all_null | 100.00% | — | 0 |
| `gh_first_commit_created_at` | all_null | 100.00% | — | 0 |
| `gh_num_commit_comments` | mostly_zero | 0.00% | 98.52% | 30 |
| `git_diff_src_churn` | mostly_zero | 0.00% | 99.08% | 225 |
| `git_diff_test_churn` | constant | 0.00% | 100.00% | 1 |
| `gh_diff_tests_added` | mostly_zero | 0.00% | 98.50% | 75 |
| `gh_diff_src_files` | mostly_zero | 0.00% | 99.06% | 65 |
| `gh_diff_doc_files` | mostly_zero | 0.00% | 100.00% | 4 |
| `gh_pushed_at` | all_null | 100.00% | — | 0 |
| `tr_log_num_test_suites_run` | all_null | 100.00% | — | 0 |
| `tr_log_num_test_suites_ok` | all_null | 100.00% | — | 0 |
| `tr_log_num_test_suites_failed` | all_null | 100.00% | — | 0 |

## Columns

| # | Column | DuckDB type | Grain | Availability | Role | Null rate | Distinct | Varying builds | Stats |
|---:|---|---|---|---|---|---:|---:|---:|---|
| 1 | `tr_build_id` | BIGINT | build | pre_build | identifier | 0.00% | 200,561 | 0 |  |
| 2 | `gh_project_name` | VARCHAR | build | pre_build | identifier | 0.00% | 241 | 0 |  |
| 3 | `gh_is_pr` | BOOLEAN | build | pre_build | feature_candidate | 0.00% | 2 | 0 | TRUE 10.79% |
| 4 | `gh_pr_created_at` | TIMESTAMP | build | pre_build | feature_candidate | 89.21% | 16,729 | 0 | 2012-07-13 11:45:01 → 2016-08-31 21:13:27 |
| 5 | `gh_pull_req_num` | BIGINT | build | pre_build | identifier | 89.21% | 2,797 | 0 |  |
| 6 | `gh_lang` | VARCHAR | build | pre_build | context | 0.00% | 1 | 0 |  |
| 7 | `git_merged_with` | VARCHAR | build | uncertain | excluded | 89.21% | 4 | 0 |  |
| 8 | `git_branch` | VARCHAR | build | pre_build | context | 0.00% | 13,284 | 0 |  |
| 9 | `gh_num_commits_in_push` | INTEGER | build | pre_build | feature_candidate | 100.00% | 0 | 0 | all NULL |
| 10 | `gh_commits_in_push` | VARCHAR | build | pre_build | identifier | 100.00% | 0 | 0 | all NULL |
| 11 | `git_prev_commit_resolution_status` | VARCHAR | build | pre_build | feature_candidate | 0.00% | 3 | 0 |  |
| 12 | `git_prev_built_commit` | VARCHAR | build | pre_build | identifier | 21.18% | 135,262 | 0 |  |
| 13 | `tr_prev_build` | BIGINT | build | pre_build | identifier | 21.65% | 135,155 | 0 |  |
| 14 | `gh_first_commit_created_at` | TIMESTAMP | build | pre_build | feature_candidate | 100.00% | 0 | 0 | all NULL |
| 15 | `gh_team_size` | INTEGER | build | pre_build | feature_candidate | 0.00% | 86 | 0 | 0 / 10.0 / 85; zero 0.46% |
| 16 | `git_all_built_commits` | VARCHAR | build | pre_build | identifier | 0.00% | 183,119 | 0 |  |
| 17 | `git_num_all_built_commits` | INTEGER | build | pre_build | feature_candidate | 0.00% | 365 | 0 | 1 / 1.0 / 6696; zero 0.00% |
| 18 | `git_trigger_commit` | VARCHAR | build | pre_build | identifier | 0.00% | 183,119 | 0 |  |
| 19 | `tr_virtual_merged_into` | VARCHAR | build | pre_build | identifier | 89.21% | 13,585 | 0 |  |
| 20 | `gh_num_issue_comments` | INTEGER | build | uncertain | excluded | 89.21% | 44 | 0 | 0 / 0.0 / 78; zero 70.69% |
| 21 | `gh_num_commit_comments` | INTEGER | build | uncertain | excluded | 0.00% | 30 | 0 | 0 / 0.0 / 82; zero 98.52% |
| 22 | `gh_num_pr_comments` | INTEGER | build | uncertain | excluded | 89.21% | 75 | 0 | 0 / 0.0 / 211; zero 86.60% |
| 23 | `git_diff_src_churn` | BIGINT | build | pre_build | feature_candidate | 0.00% | 225 | 0 | 0 / 0.0 / 5205; zero 99.08% |
| 24 | `git_diff_test_churn` | BIGINT | build | pre_build | feature_candidate | 0.00% | 1 | 0 | 0 / 0.0 / 0; zero 100.00% |
| 25 | `gh_diff_files_added` | INTEGER | build | pre_build | feature_candidate | 0.00% | 314 | 0 | 0 / 0.0 / 19892; zero 79.63% |
| 26 | `gh_diff_files_deleted` | INTEGER | build | pre_build | feature_candidate | 0.00% | 254 | 0 | 0 / 0.0 / 8450; zero 93.32% |
| 27 | `gh_diff_files_modified` | INTEGER | build | pre_build | feature_candidate | 0.00% | 428 | 0 | 0 / 2.0 / 2596; zero 2.53% |
| 28 | `gh_diff_tests_added` | INTEGER | build | pre_build | feature_candidate | 0.00% | 75 | 0 | 0 / 0.0 / 560; zero 98.50% |
| 29 | `gh_diff_tests_deleted` | INTEGER | build | pre_build | feature_candidate | 0.00% | 238 | 0 | 0 / 0.0 / 1824; zero 90.36% |
| 30 | `gh_diff_src_files` | INTEGER | build | pre_build | feature_candidate | 0.00% | 65 | 0 | 0 / 0.0 / 295; zero 99.06% |
| 31 | `gh_diff_doc_files` | INTEGER | build | pre_build | feature_candidate | 0.00% | 4 | 0 | 0 / 0.0 / 4; zero 100.00% |
| 32 | `gh_diff_other_files` | INTEGER | build | pre_build | feature_candidate | 0.00% | 533 | 0 | 0 / 2.0 / 19908; zero 0.45% |
| 33 | `gh_num_commits_on_files_touched` | INTEGER | build | pre_build | feature_candidate | 0.00% | 4,677 | 0 | 0 / 47.0 / 8866; zero 1.77% |
| 34 | `gh_sloc` | BIGINT | build | pre_build | feature_candidate | 0.00% | 64,214 | 0 | 1 / 41783.0 / 808399; zero 0.00% |
| 35 | `gh_test_lines_per_kloc` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 92,022 | 0 | 0.0 / 297.2117 / 414076.9231; zero 5.26% |
| 36 | `gh_test_cases_per_kloc` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 80,900 | 0 | 0.0 / 9.2071 / 6192.3077; zero 8.40% |
| 37 | `gh_asserts_cases_per_kloc` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 82,303 | 0 | 0.0 / 33.2795 / 62000.0; zero 8.65% |
| 38 | `gh_by_core_team_member` | BOOLEAN | build | pre_build | feature_candidate | 0.00% | 2 | 0 | TRUE 79.76% |
| 39 | `gh_description_complexity` | INTEGER | build | pre_build | feature_candidate | 89.24% | 317 | 0 | 1 / 17.0 / 1002; zero 0.00% |
| 40 | `gh_pushed_at` | TIMESTAMP | build | pre_build | feature_candidate | 100.00% | 0 | 0 | all NULL |
| 41 | `gh_build_started_at` | TIMESTAMP | build | pre_build | feature_candidate | 0.00% | 198,292 | 0 | 2012-01-27 14:36:49 → 2016-08-31 23:59:27 |
| 42 | `gh_repo_age` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 118,693 | 0 | 0.0 / 1044.07 / 6504.88; zero 0.01% |
| 43 | `gh_repo_num_commits` | BIGINT | build | pre_build | feature_candidate | 0.00% | 21,767 | 0 | 0 / 2621.0 / 44393; zero 0.00% |
| 44 | `tr_job_id` | BIGINT | job | pre_build | identifier | 0.00% | 401,767 | 76,115 |  |
| 45 | `tr_build_number` | BIGINT | build | pre_build | identifier | 0.00% | 8,408 | 0 |  |
| 46 | `tr_log_lan` | VARCHAR | job | post_build | outcome | 0.00% | 11 | 698 |  |
| 47 | `tr_log_status` | VARCHAR | job | post_build | outcome | 0.00% | 5 | 14,928 |  |
| 48 | `tr_log_setup_time` | DOUBLE | job | post_build | outcome | 22.81% | 708 | 31,407 | 0.0 / 2.0 / 4736.0; zero 13.11% |
| 49 | `tr_log_analyzer` | VARCHAR | job | post_build | outcome | 0.00% | 6 | 3,920 |  |
| 50 | `tr_log_frameworks` | VARCHAR | job | post_build | outcome | 54.52% | 4 | 8,220 |  |
| 51 | `tr_log_bool_tests_ran` | BOOLEAN | job | post_build | outcome | 0.00% | 2 | 8,220 | TRUE 45.48% |
| 52 | `tr_log_bool_tests_failed` | BOOLEAN | job | post_build | outcome | 54.52% | 2 | 11,145 | TRUE 8.89% |
| 53 | `tr_log_num_tests_ok` | INTEGER | job | post_build | outcome | 61.42% | 3,019 | 13,799 | 0 / 16.0 / 733960; zero 8.27% |
| 54 | `tr_log_num_tests_failed` | INTEGER | job | post_build | outcome | 55.15% | 91 | 11,408 | 0 / 0.0 / 697; zero 92.40% |
| 55 | `tr_log_num_tests_run` | INTEGER | job | post_build | outcome | 64.08% | 2,883 | 12,810 | 0 / 20.0 / 733960; zero 0.00% |
| 56 | `tr_log_num_tests_skipped` | INTEGER | job | post_build | outcome | 61.42% | 164 | 9,604 | 0 / 0.0 / 3159; zero 75.28% |
| 57 | `tr_log_num_test_suites_run` | INTEGER | job | post_build | outcome | 100.00% | 0 | 0 | all NULL |
| 58 | `tr_log_num_test_suites_ok` | INTEGER | job | post_build | outcome | 100.00% | 0 | 0 | all NULL |
| 59 | `tr_log_num_test_suites_failed` | INTEGER | job | post_build | outcome | 100.00% | 0 | 0 | all NULL |
| 60 | `tr_log_tests_failed` | VARCHAR | job | post_build | outcome | 96.60% | 2,804 | 3,796 |  |
| 61 | `tr_log_testduration` | DOUBLE | job | post_build | outcome | 58.54% | 60,896 | 40,063 | 0.0 / 65.57 / 14019.06; zero 8.31% |
| 62 | `tr_log_buildduration` | DOUBLE | job | post_build | outcome | 57.06% | 8,898 | 30,732 | 0.0 / 127.0 / 3598.0; zero 5.90% |
| 63 | `tr_original_commit` | VARCHAR | build | pre_build | identifier | 0.00% | 190,813 | 0 |  |
| 64 | `tr_duration` | INTEGER | build | post_build | outcome | 0.05% | 11,104 | 0 | 0 / 575.0 / 354680; zero 0.14% |
| 65 | `tr_status` | VARCHAR | build | post_build | label | 0.00% | 4 | 0 |  |
| 66 | `tr_jobs` | VARCHAR | build | pre_build | identifier | 0.00% | 200,561 | 0 |  |

## Meanings and availability justifications

| Column | Meaning | Availability — justification |
|---|---|---|
| `tr_build_id` | Travis CI build id of the analysed build. | **pre_build** — Assigned when the build is created. Identifier — not a model feature. |
| `gh_project_name` | GitHub repository name (owner/repo). | **pre_build** — Known before the build. Identifier — not a model feature. |
| `gh_is_pr` | Whether a pull request triggered the build. | **pre_build** — Property of the trigger event. |
| `gh_pr_created_at` | For PR builds: when the pull request was opened (UTC). | **pre_build** — A PR build needs an open PR, so the PR predates it. Measured exceptions (PR created after the build started; data errors) are counted under Invariant checks — features derived from it (e.g. PR age) must tolerate negative values. |
| `gh_pull_req_num` | For PR builds: the pull request number on GitHub. | **pre_build** — Known at trigger time. Identifier — not a model feature. |
| `gh_lang` | Dominant repository language according to GitHub (partition key). | **pre_build** — Repository metadata, used to select the Java subset. Context — not a model feature. |
| `git_merged_with` | For commits on a PR: how that PR was eventually closed (merge button, manual merge, ...). | **uncertain** — Describes how the PR was closed, which can happen after the build — possible future information. |
| `git_branch` | Branch that was built. | **pre_build** — Known at trigger time. Context — not a model feature. |
| `gh_num_commits_in_push` | Number of commits in the push that triggered the build (from GHTorrent push events; nil when no push event was recorded). | **pre_build** — The triggering push precedes the build. |
| `gh_commits_in_push` | The commits contained in the triggering push. | **pre_build** — The triggering push precedes the build. Identifier — not a model feature. |
| `git_prev_commit_resolution_status` | Why the backwards walk for the previously built commit stopped: no previous build, previous build found, or a merge point reached. | **pre_build** — Derived from git history up to the built commit. |
| `git_prev_built_commit` | Commit of the previous build on the linearised history (nil at a merge point). | **pre_build** — Refers to an earlier commit. Identifier — not a model feature. |
| `tr_prev_build` | Build triggered by git_prev_built_commit (nil at a merge point). | **pre_build** — The id is known at trigger time, but that build's outcome may not be (use the finished-before rule). Identifier — not a model feature. |
| `gh_first_commit_created_at` | Timestamp of the first commit in the triggering push (UTC; nil when no push event was recorded). | **pre_build** — The triggering push precedes the build. |
| `gh_team_size` | Developers who committed directly or merged PRs in the 3 months up to the build trigger. | **pre_build** — The docs bound it to the window ending when the build was triggered. |
| `git_all_built_commits` | All commits built by this build, walking back to (excluding) the previous built commit or up to a merge point; the basis of the git_diff_* / gh_diff_* columns. | **pre_build** — List of commit SHAs that exist before the build. Identifier — not a model feature. |
| `git_num_all_built_commits` | Number of commits in git_all_built_commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `git_trigger_commit` | Commit that triggered the build. | **pre_build** — Known at trigger time. Identifier — not a model feature. |
| `tr_virtual_merged_into` | For PR builds: the target-branch commit the PR commit was merged into for testing. | **pre_build** — Known at trigger time. Identifier — not a model feature. |
| `gh_num_issue_comments` | For commits linked to a PR: number of discussion comments on that PR. | **uncertain** — Not time-bounded to the build start in the docs; may count comments written after the build — possible future information. |
| `gh_num_commit_comments` | Number of GitHub comments on the built commits. | **uncertain** — Not time-bounded to the build start in the docs; may count comments written after the build — possible future information. |
| `gh_num_pr_comments` | For PR builds: number of code-review comments on the pull request. | **uncertain** — Not time-bounded to the build start in the docs; may count review comments written after the build — possible future information. |
| `git_diff_src_churn` | Lines of production code changed across the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `git_diff_test_churn` | Lines of test code changed across the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_files_added` | Files added by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_files_deleted` | Files deleted by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_files_modified` | Files modified by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_tests_added` | Test cases added by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_tests_deleted` | Test cases deleted by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_src_files` | Production source files changed by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_doc_files` | Documentation files changed by the built commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_diff_other_files` | Changed files that are neither source code nor documentation. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_num_commits_on_files_touched` | Distinct commits in the 3 months up to the trigger that touched the files changed by the built commits (how active that code area is). | **pre_build** — The docs bound it to the window ending when the build was triggered. |
| `gh_sloc` | Executable production source lines in the whole repository (comments excluded). | **pre_build** — Repository state at the built commit. |
| `gh_test_lines_per_kloc` | Test density: lines of test code per 1,000 gh_sloc. | **pre_build** — Repository state at the built commit. |
| `gh_test_cases_per_kloc` | Test density: test cases per 1,000 gh_sloc. | **pre_build** — Repository state at the built commit. |
| `gh_asserts_cases_per_kloc` | Assert density: assertions per 1,000 gh_sloc. | **pre_build** — Repository state at the built commit. |
| `gh_by_core_team_member` | Whether the commit author committed or merged at least once in the 3 months before the commit (core team member). | **pre_build** — The docs bound it to the 3 months before the commit. |
| `gh_description_complexity` | For PR builds: total words in the PR title and description. | **pre_build** — The PR exists before its builds. Caveat: the docs do not say the text was captured at build time, so later edits cannot be ruled out. |
| `gh_pushed_at` | Timestamp of the triggering push as reported by GitHub (UTC). | **pre_build** — The push precedes the build. |
| `gh_build_started_at` | Build trigger/start timestamp as reported by Travis (UTC); the docs describe it as the Travis-provided time of the triggering push. | **pre_build** — Trigger time; used for ordering, history windows and time features. UTC confirmed in M2 review: equal to the secondary archive's explicit-UTC started_at for all 27,055 builds joined on tr_build_id. Caveat: a restart overwrites it (DATASET_AUDIT §4.3). |
| `gh_repo_age` | Not described on the official data-format page. | **pre_build** — Measured to advance ~1 day per day of gh_build_started_at within a project (see evidence), i.e. repository age at build time. |
| `gh_repo_num_commits` | Not described on the official data-format page. | **pre_build** — Measured to vary over time within a project (see evidence), consistent with a commit count at build time rather than a crawl-time snapshot. |
| `tr_job_id` | Travis job id of the analysed job (one CSV row per job). | **pre_build** — Assigned when the build's jobs are created. Identifier — not a model feature. |
| `tr_build_number` | Per-project serial build number. | **pre_build** — Assigned when the build is created. Identifier — not a model feature. |
| `tr_log_lan` | Primary programming language detected by build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_status` | Overall return status detected by build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_setup_time` | Seconds of setup before the script phase, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_analyzer` | Which build-log analyzer processed the log. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_frameworks` | Test frameworks detected by build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_bool_tests_ran` | Whether tests ran, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_bool_tests_failed` | Whether tests failed, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_tests_ok` | Number of passing tests, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_tests_failed` | Number of failing tests, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_tests_run` | Total number of tests run, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_tests_skipped` | Number of skipped tests, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_test_suites_run` | Not described on the official data-format page (the name suggests test suites run, from build-log analysis; unverified). | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_test_suites_ok` | Not described on the official data-format page (the name suggests passing test suites, from build-log analysis; unverified). | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_num_test_suites_failed` | Not described on the official data-format page (the name suggests failing test suites, from build-log analysis; unverified). | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_tests_failed` | Names of the failing tests, from build-log analysis (free text). | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_testduration` | Seconds spent running tests, from build-log analysis. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_log_buildduration` | Seconds spent in the build tool (e.g. Maven, Ant), from build-log analysis; includes the test phase. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_original_commit` | Commit SHA Travis built; for PRs possibly a virtual merge commit not in the repository. | **pre_build** — Known at trigger time. Identifier — not a model feature. |
| `tr_duration` | Full build duration reported by the Travis API. Summed job time, not wall-clock (DATASET_AUDIT §4.1; equal to that archive's duration for 26,990 of 27,055 builds joined in M2 review), so started_at + tr_duration only bounds the finish time. | **post_build** — Only known once the build finished; usable only as lagged history of finished builds. |
| `tr_status` | Build status from the Travis API (passed, failed, errored, canceled, ...). | **post_build** — The prediction target (ADR-0005); only known once the build finished. |
| `tr_jobs` | Travis job ids of the build, '#'-separated. | **pre_build** — Jobs are created together with the build. Identifier — not a model feature. |

## Evidence for undocumented columns

- `gh_repo_age`: per-project OLS slope of gh_repo_age on days since epoch of gh_build_started_at (Java projects with >= 20 builds): median slope **0.9999** days per day (p10 0.9952, p90 1.0009, 215 projects) — consistent with the repository's age in days at build time.
- `gh_repo_num_commits`: share of consecutive builds of a project (ordered by gh_build_started_at) whose value does not decrease: **89.33%** of 200,320 pairs; 240 of 241 projects have more than one value — so it is not a single crawl-time snapshot per project. Exact definition unknown.
  - same, restricted to non-PR builds on git_branch = 'master': non-decreasing in **98.97%** and strictly increasing in 96.82% of 103,372 pairs; the per-project slope over time is positive in 215 of 215 projects (>= 20 builds) — consistent with a commit count at build time.
- `tr_log_num_test_suites_*`: meaning inferred from the name only (unverified).

## Invariant checks (Java builds, measured)

Ordering facts that leakage-safe features rely on. Violations are data errors in the dump; downstream code (dbt tests, feature functions) must tolerate or exclude them.

| Rule | Violations | Of |
|---|---:|---:|
| `gh_pr_created_at <= gh_build_started_at (a PR build needs an open PR)` | 12 (up to 6913.9714 h after the build started) | 21,641 PR builds |
| `tr_prev_build < tr_build_id (the previous build was created earlier)` | 0 | 157,147 builds with a previous build |
