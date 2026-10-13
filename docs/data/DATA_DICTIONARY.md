# Data dictionary — TravisTorrent (Figshare `travistorrent_5_3_2016`)

> **Generated** by `python -m rl_pipeline build_data_dictionary --dataset travistorrent-5-3-2016` from the converted Parquet of the real dataset. Do not edit by hand; every number below is computed. Machine-readable twin: `docs/data/data_dictionary.json`.

## Source

- Dataset key `travistorrent-5-3-2016`. File: `travistorrent_5_3_2016.csv.gz` · 81,902,127 bytes · md5 `4713d8b73b667a2c375b89c20f531fc0` (= dataset version)
- Figshare DOI [10.6084/m9.figshare.19314170](https://doi.org/10.6084/m9.figshare.19314170) · download URL `https://ndownloader.figshare.com/files/34303127`
- License: **CC BY 4.0**. Attribution: Beller, M., Gousios, G., Zaidman, A. TravisTorrent: Synthesizing Travis CI and GitHub for Full-Stack Research on Continuous Integration. MSR 2017, DOI 10.1109/MSR.2017.24. Data: Figshare DOI 10.6084/m9.figshare.19314170 (CC BY 4.0).
- Column meanings paraphrase the official data-format page (https://raw.githubusercontent.com/TestRoots/travistorrent-site/7466b3d2ca08f1f4b792007705ae5bafb33a8224/page_dataformat.md, commit `7466b3d2ca08`). Last page version describing this 56-column format (2016-11-09). The first version (commit 1ca2ce08d42e, 2016-07-28) defines gh_first_commit_created_at, gh_team_size, git_commits, the comment counts, the change columns, gh_tests_added/deleted, gh_src/doc/other_files, gh_commits_on_files_touched and gh_description_complexity differently. Both post-date the dump (dated 2016-03-05 by its file name); measured evidence below decides where possible.
- Timestamps: TIMESTAMP (no zone). tr_started_at is UTC wall-clock (measured: equal to the secondary archive's explicit-UTC started_at on every joined build). gh_first_commit_created_at is tr_started_at rendered in Europe/London local time (measured), i.e. not UTC.

## Counts

| Scope | Job rows | Distinct `tr_job_id` | Builds | Projects |
|---|---:|---:|---:|---:|
| All languages | 2,640,825 | 1,800,390 | 360,770 | 1,300 |
| `gh_lang = java` | 256,055 | 234,391 | 128,819 | 402 |
| `gh_lang = ruby` | 2,384,770 | 1,565,999 | 231,951 | 898 |

Java `tr_started_at` range (build level, UTC): **2011-06-20 13:04:06 → 2015-08-19 02:52:38**.

### Java label distribution (`tr_status`, one row per build)

| tr_status | Builds | Share |
|---|---:|---:|
| `passed` | 91,282 | 70.86% |
| `failed` | 22,068 | 17.13% |
| `errored` | 15,267 | 11.85% |
| `canceled` | 199 | 0.15% |
| `started` | 3 | 0.00% |

Label rule (ADR-0005/0017): `failed`/`errored` → 1, `passed` → 0, `canceled` excluded. Any other status present above (e.g. `started`) is excluded as well (no observed outcome).

## Spec names → real 2016 column names

The spec's names are this release's own names. Every target below was checked against the real header.

| Spec name | Real column | Renamed | In header | Availability |
|---|---|---|---|---|
| `gh_src_churn` | `gh_src_churn` | no | yes | pre_build |
| `gh_test_churn` | `gh_test_churn` | no | yes | pre_build |
| `gh_files_added` | `gh_files_added` | no | yes | pre_build |
| `gh_files_deleted` | `gh_files_deleted` | no | yes | pre_build |
| `gh_files_modified` | `gh_files_modified` | no | yes | pre_build |
| `gh_tests_added` | `gh_tests_added` | no | yes | pre_build |
| `gh_tests_deleted` | `gh_tests_deleted` | no | yes | pre_build |
| `gh_src_files` | `gh_src_files` | no | yes | pre_build |
| `gh_doc_files` | `gh_doc_files` | no | yes | pre_build |
| `gh_other_files` | `gh_other_files` | no | yes | pre_build |
| `gh_commits_on_files_touched` | `gh_commits_on_files_touched` | no | yes | pre_build |
| `gh_sloc` | `gh_sloc` | no | yes | pre_build |
| `gh_test_lines_per_kloc` | `gh_test_lines_per_kloc` | no | yes | pre_build |
| `gh_test_cases_per_kloc` | `gh_test_cases_per_kloc` | no | yes | pre_build |
| `gh_asserts_cases_per_kloc` | `gh_asserts_cases_per_kloc` | no | yes | pre_build |
| `gh_team_size` | `gh_team_size` | no | yes | pre_build |
| `gh_by_core_team_member` | `gh_by_core_team_member` | no | yes | pre_build |
| `gh_description_complexity` | `gh_description_complexity` | no | yes | uncertain |
| `tr_started_at` | `tr_started_at` | no | yes | pre_build |
| `tr_prev_build` | `tr_prev_build` | no | yes | pre_build |

## rl_core build inputs → columns

How each `rl_core.features.change.BUILD_INPUT_FIELDS` input is fed from this release (applied in staging/marts, M3). `direct`: same concept; `approximate`: a related definition (see note); `none`: not available, the input stays unknown (NaN). Rates are measured on Java builds.

| Build input | Column | Status | Availability | Java null | Java zero | Note |
|---|---|---|---|---:|---:|---|
| `src_churn` | `gh_src_churn` | direct | pre_build | 0.00% | 32.21% |  |
| `test_churn` | `gh_test_churn` | direct | pre_build | 0.00% | 70.69% |  |
| `files_added` | `gh_files_added` | direct | pre_build | 0.00% | 78.65% |  |
| `files_deleted` | `gh_files_deleted` | direct | pre_build | 0.00% | 92.84% |  |
| `files_modified` | `gh_files_modified` | direct | pre_build | 0.00% | 2.79% |  |
| `tests_added` | `gh_tests_added` | approximate | pre_build | 0.00% | 97.95% | page versions disagree: lines vs cases |
| `tests_deleted` | `gh_tests_deleted` | approximate | pre_build | 0.00% | 88.41% | page versions disagree: lines vs cases |
| `src_files` | `gh_src_files` | direct | pre_build | 0.00% | 26.56% |  |
| `doc_files` | `gh_doc_files` | direct | pre_build | 0.00% | 94.43% |  |
| `other_files` | `gh_other_files` | direct | pre_build | 0.00% | 60.58% |  |
| `sloc` | `gh_sloc` | direct | pre_build | 0.00% | 0.00% |  |
| `test_lines_per_kloc` | `gh_test_lines_per_kloc` | direct | pre_build | 0.00% | 4.96% |  |
| `test_cases_per_kloc` | `gh_test_cases_per_kloc` | direct | pre_build | 0.00% | 7.99% |  |
| `asserts_per_kloc` | `gh_asserts_cases_per_kloc` | direct | pre_build | 0.00% | 6.74% | renamed |
| `num_commits_in_push` | — | none | — | — | — | no push-level count (GHTorrent push events); excluded per ADR-0004 |
| `num_all_built_commits` | `git_num_commits` | approximate | pre_build | 0.00% | 0.00% | entries of git_commits, which excludes the built commit (see evidence) |
| `team_size` | `gh_team_size` | direct | pre_build | 0.00% | 0.41% |  |
| `by_core_team_member` | `gh_by_core_team_member` | direct | pre_build | 0.00% | — |  |
| `is_pr` | `gh_is_pr` | direct | pre_build | 0.00% | — |  |
| `description_complexity` | `gh_description_complexity` | direct | uncertain | 85.92% | 0.00% | uncertain: excluded from model inputs by default |
| `commits_on_files_touched` | `gh_commits_on_files_touched` | direct | pre_build | 0.00% | 5.50% |  |
| `repo_age_days` | — | none | — | — | — | no repository-age column; gh_first_commit_created_at is a copy of tr_started_at |

## Availability summary

- `pre_build` (known when the build is triggered): 35 columns, of which 22 are feature candidates and the rest identifiers/context/excluded (never model features).
- `post_build` (label, duration, build-log analysis): 15 columns — forbidden for the current build; usable only as lagged history of builds that had finished.
- `uncertain` (**excluded from model inputs by default**): `git_merged_with`, `gh_num_issue_comments`, `gh_num_commit_comments`, `gh_num_pr_comments`, `gh_description_complexity`, `tr_lan`.
- `model_input_default` is true for 22 feature candidates. Feature candidates measured as all-NULL or constant in the Java subset are excluded: none.

## Measurement

- Subset: `gh_lang = 'java'`. Build-grain columns: one row per tr_build_id: a job row whose tr_job_id is listed in the build's own tr_jobs first, then the lowest (tr_job_id, row); job-grain columns: every job row (raw is not deduplicated).
- Null = SQL NULL after conversion (raw empty field, the 'NA' token, or a value that failed its strict cast — see Cast failures). Distinct = exact count(DISTINCT), NULL excluded.
- *Varying builds* = Java builds whose jobs disagree on the column (different values, or NULL mixed with a value) — a measured check of the declared grain.
- Stats: min / median / max (and the share of zeros) for numeric feature/outcome columns, min → max for timestamps, TRUE share for booleans. Free-text values are never shown.

## Data-quality flags (Java, measured)

`all_null`: no value at all · `constant`: a single distinct value · `mostly_zero`: ≥ 95% of non-null values are 0. Flagged columns carry little or no signal in this dump; downstream feature selection must take this into account.

| Column | Flags | Null rate | Zero share | Distinct |
|---|---|---:|---:|---:|
| `gh_num_issue_comments` | mostly_zero | 0.00% | 97.38% | 26 |
| `gh_num_commit_comments` | mostly_zero | 0.00% | 95.98% | 58 |
| `gh_num_pr_comments` | mostly_zero | 0.00% | 98.71% | 44 |
| `gh_tests_added` | mostly_zero | 0.00% | 97.95% | 81 |

## Cast failures (conversion gate)

Non-missing raw values that failed their strict cast and became NULL (all languages, every job row, per the conversion manifest; the gate allows at most 0.10% per column).

| Column | Failed | Of non-missing |
|---|---:|---:|
| `git_num_committers` | 10 | 2,640,823 |
| `tr_num_jobs` | 10 | 2,640,823 |
| `tr_prev_build` | 10 | 2,640,823 |
| `tr_purebuildduration` | 12 | 273,377 |
| `tr_testduration` | 10 | 1,563,484 |
| `tr_tests_failed` | 12 | 2,640,825 |
| `tr_tests_ran` | 12 | 2,640,825 |

## Columns

| # | Column | DuckDB type | Grain | Availability | Role | Null rate | Distinct | Varying builds | Stats |
|---:|---|---|---|---|---|---:|---:|---:|---|
| 1 | `row` | INTEGER | job | pre_build | identifier | 0.00% | 256,055 | 57,289 |  |
| 2 | `git_commit` | VARCHAR | build | pre_build | identifier | 0.00% | 123,828 | 0 |  |
| 3 | `gh_project_name` | VARCHAR | build | pre_build | identifier | 0.00% | 402 | 0 |  |
| 4 | `gh_is_pr` | BOOLEAN | build | pre_build | feature_candidate | 0.00% | 2 | 0 | TRUE 14.08% |
| 5 | `git_merged_with` | VARCHAR | build | uncertain | excluded | 85.92% | 4 | 0 |  |
| 6 | `gh_lang` | VARCHAR | build | pre_build | context | 0.00% | 1 | 0 |  |
| 7 | `git_branch` | VARCHAR | build | pre_build | context | 0.00% | 8,874 | 0 |  |
| 8 | `gh_first_commit_created_at` | TIMESTAMP | build | pre_build | excluded | 0.00% | 128,146 | 0 | 2011-06-20 14:04:06 → 2015-08-19 03:52:38 |
| 9 | `gh_team_size` | INTEGER | build | pre_build | feature_candidate | 0.00% | 55 | 0 | 0 / 6.0 / 54; zero 0.41% |
| 10 | `git_commits` | VARCHAR | build | pre_build | identifier | 0.00% | 113,653 | 0 |  |
| 11 | `git_num_commits` | INTEGER | build | pre_build | feature_candidate | 0.00% | 84 | 0 | 1 / 1.0 / 553; zero 0.00% |
| 12 | `gh_num_issue_comments` | INTEGER | build | uncertain | excluded | 0.00% | 26 | 0 | 0 / 0.0 / 39; zero 97.38% |
| 13 | `gh_num_commit_comments` | INTEGER | build | uncertain | excluded | 0.00% | 58 | 0 | 0 / 0.0 / 238; zero 95.98% |
| 14 | `gh_num_pr_comments` | INTEGER | build | uncertain | excluded | 0.00% | 44 | 0 | 0 / 0.0 / 108; zero 98.71% |
| 15 | `gh_src_churn` | BIGINT | build | pre_build | feature_candidate | 0.00% | 3,499 | 0 | 0 / 10.0 / 196663; zero 32.21% |
| 16 | `gh_test_churn` | BIGINT | build | pre_build | feature_candidate | 0.00% | 1,738 | 0 | 0 / 0.0 / 50566; zero 70.69% |
| 17 | `gh_files_added` | INTEGER | build | pre_build | feature_candidate | 0.00% | 215 | 0 | 0 / 0.0 / 4212; zero 78.65% |
| 18 | `gh_files_deleted` | INTEGER | build | pre_build | feature_candidate | 0.00% | 171 | 0 | 0 / 0.0 / 4072; zero 92.84% |
| 19 | `gh_files_modified` | INTEGER | build | pre_build | feature_candidate | 0.00% | 378 | 0 | 0 / 2.0 / 6369; zero 2.79% |
| 20 | `gh_tests_added` | INTEGER | build | pre_build | feature_candidate | 0.00% | 81 | 0 | 0 / 0.0 / 996; zero 97.95% |
| 21 | `gh_tests_deleted` | INTEGER | build | pre_build | feature_candidate | 0.00% | 212 | 0 | 0 / 0.0 / 3075; zero 88.41% |
| 22 | `gh_src_files` | INTEGER | build | pre_build | feature_candidate | 0.00% | 431 | 0 | 0 / 1.0 / 6372; zero 26.56% |
| 23 | `gh_doc_files` | INTEGER | build | pre_build | feature_candidate | 0.00% | 117 | 0 | 0 / 0.0 / 1641; zero 94.43% |
| 24 | `gh_other_files` | INTEGER | build | pre_build | feature_candidate | 0.00% | 211 | 0 | 0 / 0.0 / 4213; zero 60.58% |
| 25 | `gh_commits_on_files_touched` | INTEGER | build | pre_build | feature_candidate | 0.00% | 410 | 0 | 0 / 11.0 / 878; zero 5.50% |
| 26 | `gh_sloc` | BIGINT | build | pre_build | feature_candidate | 0.00% | 44,856 | 0 | 14 / 25872.0 / 742475; zero 0.00% |
| 27 | `gh_test_lines_per_kloc` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 63,469 | 0 | 0.0 / 342.1327 / 7065.8363; zero 4.96% |
| 28 | `gh_test_cases_per_kloc` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 56,461 | 0 | 0.0 / 10.694 / 318.5484; zero 7.99% |
| 29 | `gh_asserts_cases_per_kloc` | DOUBLE | build | pre_build | feature_candidate | 0.00% | 57,913 | 0 | 0.0 / 37.1868 / 596.9425; zero 6.74% |
| 30 | `gh_by_core_team_member` | BOOLEAN | build | pre_build | feature_candidate | 0.00% | 2 | 0 | TRUE 78.28% |
| 31 | `gh_description_complexity` | INTEGER | build | uncertain | excluded | 85.92% | 299 | 0 | 1 / 16.0 / 1538; zero 0.00% |
| 32 | `tr_build_id` | BIGINT | build | pre_build | identifier | 0.00% | 128,819 | 0 |  |
| 33 | `gh_pull_req_num` | BIGINT | build | pre_build | identifier | 85.92% | 1,890 | 0 |  |
| 34 | `tr_status` | VARCHAR | build | post_build | label | 0.00% | 5 | 0 |  |
| 35 | `tr_duration` | INTEGER | build | post_build | outcome | 0.01% | 7,312 | 0 | 0 / 452.0 / 34785; zero 0.00% |
| 36 | `tr_started_at` | TIMESTAMP | build | pre_build | feature_candidate | 0.00% | 128,146 | 0 | 2011-06-20 13:04:06 → 2015-08-19 02:52:38 |
| 37 | `tr_jobs` | VARCHAR | build | pre_build | identifier | 0.00% | 128,819 | 0 |  |
| 38 | `tr_build_number` | BIGINT | build | pre_build | identifier | 0.00% | 6,268 | 8,943 |  |
| 39 | `tr_job_id` | BIGINT | job | pre_build | identifier | 0.00% | 234,391 | 57,289 |  |
| 40 | `tr_lan` | VARCHAR | job | uncertain | identifier | 0.00% | 3 | 99 |  |
| 41 | `tr_setup_time` | DOUBLE | job | post_build | outcome | 44.16% | 503 | 12,292 | 0.0 / 1.0 / 25461.0; zero 38.26% |
| 42 | `tr_analyzer` | VARCHAR | job | post_build | outcome | 0.00% | 4 | 2,848 |  |
| 43 | `tr_frameworks` | VARCHAR | job | post_build | outcome | 50.55% | 3 | 3,596 |  |
| 44 | `tr_tests_ok` | INTEGER | job | post_build | outcome | 56.50% | 1,911 | 6,441 | -15 / 24.0 / 724958; zero 7.41% |
| 45 | `tr_tests_fail` | INTEGER | job | post_build | outcome | 38.36% | 117 | 5,172 | 0 / 0.0 / 2037; zero 94.34% |
| 46 | `tr_tests_run` | INTEGER | job | post_build | outcome | 56.50% | 1,798 | 5,914 | 0 / 25.0 / 724959; zero 6.64% |
| 47 | `tr_tests_skipped` | INTEGER | job | post_build | outcome | 56.50% | 185 | 4,437 | 0 / 0.0 / 3159; zero 74.93% |
| 48 | `tr_failed_tests` | VARCHAR | job | post_build | outcome | 96.72% | 2,385 | 1,961 |  |
| 49 | `tr_testduration` | DOUBLE | job | post_build | outcome | 53.76% | 32,721 | 28,973 | 0.0 / 19.39 / 14019.06; zero 7.46% |
| 50 | `tr_purebuildduration` | DOUBLE | job | post_build | outcome | 45.17% | 8,604 | 22,998 | 0.0 / 46.11 / 3598.0; zero 28.13% |
| 51 | `tr_tests_ran` | BOOLEAN | job | post_build | outcome | 0.00% | 2 | 3,596 | TRUE 49.45% |
| 52 | `tr_tests_failed` | BOOLEAN | job | post_build | outcome | 0.00% | 2 | 2,123 | TRUE 3.88% |
| 53 | `git_num_committers` | INTEGER | build | pre_build | feature_candidate | 0.00% | 22 | 2 | 0 / 1.0 / 24; zero 0.00% |
| 54 | `tr_num_jobs` | INTEGER | build | pre_build | feature_candidate | 0.00% | 21 | 2 | 1 / 1.0 / 42; zero 0.00% |
| 55 | `tr_prev_build` | BIGINT | job | pre_build | identifier | 0.00% | 40,772 | 16,169 |  |
| 56 | `tr_ci_latency` | BIGINT | build | post_build | outcome | 0.00% | 21,883 | 0 | -58865485 / 60.0 / 44463310; zero 0.02% |

## Meanings and availability justifications

| Column | Meaning | Availability — justification |
|---|---|---|
| `row` | Row number of the job row in the dump; documented as the unique identifier of a build job. | **pre_build** — Dump artifact (measured unique), used as the conversion's unique key and last sort column because tr_job_id is not unique in this release. Identifier — not a model feature. |
| `git_commit` | SHA-1 of the commit that triggered the build. | **pre_build** — Known at trigger time. Identifier — not a model feature. |
| `gh_project_name` | GitHub repository name (owner/repo). | **pre_build** — Known before the build. Identifier — not a model feature. |
| `gh_is_pr` | Whether the build was triggered as part of a pull request. | **pre_build** — Property of the trigger event. |
| `git_merged_with` | For PR builds: how the pull request was merged. The page calls it the SHA-1 of the merging commit, but the values are a few categorical codes (see Distinct). | **uncertain** — Describes how the PR was eventually merged, which can happen after the build — possible future information. |
| `gh_lang` | Dominant repository language according to GitHub (partition key). | **pre_build** — Repository metadata, used to select the Java subset. Context — not a model feature. |
| `git_branch` | Branch git_commit was committed on. | **pre_build** — Known at trigger time. Context — not a model feature. |
| `gh_first_commit_created_at` | Documented as the timestamp of the first commit in the triggering push (the 2016-07-28 page version: first commit of the whole repository). Measured: a copy of tr_started_at in UK local time (GMT/BST), not a commit timestamp (see evidence). | **pre_build** — Carries no information beyond tr_started_at and is not UTC (measured, see evidence): never use it as a commit time or a feature. |
| `gh_team_size` | Developers who committed directly or merged PRs in the 3 months up to the build trigger (the 2016-07-28 page version: size of the team, no window). | **pre_build** — The later page bounds it to the window ending at the trigger; the earlier page does not state a window. |
| `git_commits` | Commits of the triggering push minus the built commit, '#'-separated (the 2016-07-28 page version: directly preceding commits without their own build). | **pre_build** — List of commit SHAs that exist before the build. Identifier — not a model feature. |
| `git_num_commits` | Number of entries in git_commits. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_num_issue_comments` | If git_commit is linked to a PR: number of discussion comments on that PR. | **uncertain** — Not time-bounded to the build start in the docs; may count comments written after the build — possible future information. |
| `gh_num_commit_comments` | Number of GitHub comments on the built commits. | **uncertain** — Not time-bounded to the build start in the docs; may count comments written after the build — possible future information. |
| `gh_num_pr_comments` | For PR builds: number of code-review comments on the pull request. | **uncertain** — Not time-bounded to the build start in the docs; may count review comments written after the build — possible future information. |
| `gh_src_churn` | Lines of production code changed by the commits built by this build (the 2016-07-28 page version: churn of git_commit only). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_test_churn` | Lines of test code changed by the commits built by this build (the 2016-07-28 page version: churn of git_commit only). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_files_added` | Files added by the commits built by this build. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_files_deleted` | Files deleted by the commits built by this build. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_files_modified` | Files modified by the commits built by this build. | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_tests_added` | Tests added by the built commits: lines of test code per the later page, test cases (e.g. @Test annotations) per the 2016-07-28 page version (see evidence). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_tests_deleted` | Tests deleted by the built commits (same two definitions as gh_tests_added). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_src_files` | Production source files changed by the built commits (the 2016-07-28 page version: production files in the whole repository; see evidence). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_doc_files` | Documentation files changed by the built commits (the 2016-07-28 page version: documentation files in the repository). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_other_files` | Changed files that are neither production code nor documentation (the 2016-07-28 page version: such files in the repository). | **pre_build** — Computed from the commits being built, which exist before the build starts. |
| `gh_commits_on_files_touched` | Distinct commits in the 3 months up to the trigger that touched the files changed by the built commits (the 2016-07-28 page version: all earlier commits on those files, no window). | **pre_build** — Counts commits that precede the trigger under either page version. |
| `gh_sloc` | Executable production source lines in the whole repository (comments excluded). | **pre_build** — Repository state at the built commit. |
| `gh_test_lines_per_kloc` | Test density: lines of test code per 1,000 gh_sloc. | **pre_build** — Repository state at the built commit. |
| `gh_test_cases_per_kloc` | Test density: test cases per 1,000 gh_sloc. | **pre_build** — Repository state at the built commit. |
| `gh_asserts_cases_per_kloc` | Assert density: assertions per 1,000 gh_sloc. | **pre_build** — Repository state at the built commit. |
| `gh_by_core_team_member` | Whether the built commit was authored by a core team member. | **pre_build** — Property of the commit author before the build. |
| `gh_description_complexity` | For PR builds: total words in the pull request title and description. | **uncertain** — Taken from the final PR text, which can be edited after the build (ADR-0017 context) — possible future information. |
| `tr_build_id` | Travis CI build id. | **pre_build** — Assigned when the build is created. Identifier — not a model feature. |
| `gh_pull_req_num` | For PR builds: the pull request number on GitHub. | **pre_build** — Known at trigger time. Identifier — not a model feature. |
| `tr_status` | Build status from the Travis API (passed, failed, errored, canceled, started). | **post_build** — The prediction target (ADR-0005); only known once the build finished. |
| `tr_duration` | Overall build duration reported by Travis, in seconds (summed job time per DATASET_AUDIT §4.1, so started_at + duration only bounds the finish time). | **post_build** — Only known once the build finished; usable only as lagged history of finished builds. |
| `tr_started_at` | Start of the build process (UTC wall-clock, verified against the secondary archive; see evidence). | **pre_build** — Trigger time; used for ordering, history windows and time features. Caveat: a restart overwrites it (DATASET_AUDIT §4.3). |
| `tr_jobs` | Travis job ids of the build, as a bracketed comma-separated list ('[id, id, ...]'). | **pre_build** — Jobs are created together with the build (the matrix is known at creation). Identifier — not a model feature. |
| `tr_build_number` | Per-project serial build number on Travis. | **pre_build** — Assigned when the build is created. Identifier — not a model feature. |
| `tr_job_id` | Travis job id of this row; documented as one of tr_jobs (not always true, see invariants). | **pre_build** — Assigned when the build's jobs are created. Identifier — not a model feature. |
| `tr_lan` | Language of the build as recognised by the build-log analyzer. | **uncertain** — Derived from the build log (formally post-build) while the language itself is a repository property already given by gh_lang; tagged uncertain. Identifier — not a model feature. |
| `tr_setup_time` | Setup time before the Travis build started, in seconds. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_analyzer` | Build-log analyzer that processed the log (e.g. java-ant, java-maven, java-gradle). | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_frameworks` | Test frameworks the analyzer recognised, '#'-separated. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_tests_ok` | Number of passed tests, if the analyzer could tell. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_tests_fail` | Number of failed tests, if the analyzer could tell. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_tests_run` | Number of tests run, if the analyzer could tell. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_tests_skipped` | Number of skipped or ignored tests, if the analyzer could tell. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_failed_tests` | Names of the failed tests, '#'-separated (free text). | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_testduration` | Seconds spent running the tests. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_purebuildduration` | Seconds the build itself ran, without Travis scheduling and provisioning. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_tests_ran` | Whether tests ran in this job. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `tr_tests_failed` | Whether tests failed in this job. | **post_build** — Produced by analysing the finished build's log; forbidden for the current build. |
| `git_num_committers` | Documented as the number of people who committed to the project; measured to count the committers of the built commits (see evidence). | **pre_build** — Measured to be bounded by git_num_commits + 1 (evidence), i.e. committers of commits that exist before the build; the page's project-wide wording is not time-bounded, so verify before relying on it. |
| `tr_num_jobs` | Number of jobs of the build (length of tr_jobs). | **pre_build** — The build matrix is known when the build is created. |
| `tr_prev_build` | Link to the previous build, given as its tr_build_id; -1 (also written -1.0) when there is none. Measured to differ between jobs of the same build. | **pre_build** — Linkage to an earlier build: its id is known at trigger time, but that build's outcome may not be (use the finished-before rule). -1 is a placeholder that staging turns into NULL. Identifier — not a model feature. |
| `tr_ci_latency` | Latency induced by Travis (scheduling, build pick-up), in seconds; -1 placeholder. | **post_build** — Only observable once Travis picked the build up, i.e. after the trigger moment. |

## Evidence (measured on the Java subset)

Where the two page versions disagree, or the data contradicts them, these measurements decide. They describe correlations in the dump, not how the authors computed it.

- **`gh_first_commit_created_at` is not a commit timestamp.** It equals `tr_started_at` rendered in Europe/London local time on 256,015 of 256,055 (99.98%) job rows; the offset to `tr_started_at` is exactly 0 h on 93,521, +1 h on 162,526 and +2 h on 8 rows. It varies per build (0 of 402 projects have a single value), so it is not a repository start date either. Use `tr_started_at` (UTC); never treat this column as a commit time or as UTC.
- **Duplicate job rows are job rows attached to more than one build.** 16,563 of 234,391 job ids occur on more than one row (38,227 rows); 16,563 of them occur under more than one `tr_build_id`. For 16,520 of the duplicated ids exactly one row lists the job in its own build's `tr_jobs` (43: none). Overall 234,184 of 256,055 (91.46%) job rows are listed in their own build; 78 builds have no listed row. Staging should keep, per job id, the row listed in its own build.
- **`gh_tests_added` / `gh_tests_deleted` behave like test-case counts, not lines.** Of 37,758 builds with test churn > 0, 28,151 have neither tests added nor deleted, and 912 of 128,819 builds have more tests added than test churn — both would be impossible for lines of test code (churn counts added + deleted lines).
- **File-type counts are per change, not per repository.** src + doc + other files <= files added + deleted + modified on 122,599 of 128,819 (95.17%) builds (equal on 103,234).
- **`git_num_commits`** equals the number of `#`-separated entries of `git_commits` on 256,055 of 256,055 (100.00%) job rows; `git_commits` contains `git_commit` on 0 rows; minimum 1.
- **`git_num_committers`** is <= `git_num_commits` + 1 on 256,053 of 256,053 (100.00%) job rows and equals 1 on 236,494 — consistent with the committers of the built commits, not the project-wide count the page describes.

## Invariant checks (Java, measured)

Facts that staging and leakage-safe features rely on. Violations are data errors or quirks of the dump; downstream code (dbt tests, feature functions) must tolerate or exclude them.

| Rule | Violations | Of | Note |
|---|---:|---|---|
| `tr_job_id is unique across job rows` | 21,664 | 256,055 job rows | extra rows beyond one per distinct tr_job_id (see evidence: duplicate job rows) |
| `tr_job_id is one of its build's tr_jobs` | 21,871 | 256,055 job rows |  |
| `tr_num_jobs = length of tr_jobs` | 0 | 256,053 job rows with both values |  |
| `build-grain columns are constant within a build` | 3 | 40 build-grain columns | varying: tr_build_number, git_num_committers, tr_num_jobs (at most 8,943 builds) |
| `tr_prev_build is a build id (not the -1 placeholder)` | 30,966 | 256,053 job rows with a value |  |
| `tr_prev_build is > 0 unless it is the -1 placeholder` | 0 | 256,053 job rows with a value |  |
| `tr_prev_build < tr_build_id (the previous build was created earlier)` | 81,249 | 225,087 job rows with tr_prev_build > 0 | order history by tr_started_at with the finished-before rule, not by this link |
| `tr_tests_ok / _fail / _run / _skipped >= 0` | 17 | 157,820 job rows with a test count | minimum -15 |
| `tr_ci_latency is not the -1 placeholder` | 6,756 | 128,819 builds with a value |  |
| `tr_ci_latency >= 0 unless it is the -1 placeholder` | 13,011 | 128,819 builds with a value | minimum -58865485 s |
| `gh_first_commit_created_at <= tr_started_at (the first commit precedes the build)` | 77,902 | 128,819 builds | every violation is an exact +1 h / +2 h offset (see evidence) |

## UTC and status cross-check (secondary archive)

Joined on the build id with `travistorrent-java-ci-build-dataset-master` (19 `repo-data-travis.csv` files, 64,736 builds, 64,628 with an explicit `UTC` start time): **7,317 Java builds** from 7 projects.

| Check | Agreeing builds |
|---|---:|
| `tr_started_at` = archive `started_at` (UTC) | 7,317 of 7,317 (100.00%) |
| … off by exactly 1 h or 2 h (a time-zone shift) | 0 of 7,317 (0.00%) |
| `tr_status` = archive `status` | 7,317 of 7,317 (100.00%) |
| `tr_duration` = archive `duration` | 7,315 of 7,317 (99.97%) |
