# Change-fact definitions (training ↔ serving contract)

The model is trained on TravisTorrent's columns (ADR-0017, 2016 release). At serving time the
ReleaseLens GitHub Action computes the **same facts from git inside the customer's CI runner**.
This file is the contract between the two. Definitions follow `TestRoots/travistorrent-tools`
(`lib/ghtorrent_extractor/*.rb`) as read during the M0 audit. Where git cannot reproduce
TravisTorrent exactly, the deviation is stated, and its size is **measured** in M5 on cloned
repositories (feature-extraction fidelity), never assumed.

## 1. Which commits a build covers ("built commits")

TravisTorrent walks the history topologically from the build's trigger commit and stops at:

- `build_found`: a commit that was itself built earlier. That commit is **excluded**.
- `merge_found`: a merge commit. The merge commit is **included**, and the walk stops.
- `no_previous_build`: the root.

All change metrics are **summed over these built commits**, not taken from one commit or one push.

**Serving (Action):** the previous built commit comes from the CI provider when available. On GitHub
Actions that is the `before` SHA of a push, or the PR base for pull requests. The Action walks
`git rev-list --topo-order <head> ^<prev>` with the same merge-stop rule. Without a previous build
(first run), only the head commit is used. The tenant's `ci_runs` history can supply the previous
built SHA instead.

## 2. Facts

| Canonical field (rl_core `BUILD_INPUT_FIELDS`) | TravisTorrent 2016 column | Definition | Serving deviation |
|---|---|---|---|
| `src_churn` | `gh_src_churn` | Lines added + deleted (unified-diff `+`/`-` lines) in **source** files over the built commits. Only files of a programming language that are **not** test files. | Language by file extension (a pinned table derived from Linguist), not Linguist itself. Binary files count 0. |
| `test_churn` | `gh_test_churn` | Same, for **test** files. Java: path matches `src/test/` or `test/` or `tests/`, or the filename matches `*Test.java`, `*Tests.java`, `Test*.java`. | Same path rules. |
| `files_added` / `files_deleted` / `files_modified` | `gh_files_added` / `gh_files_deleted` / `gh_files_modified` | Number of **unique** file paths with status added / removed / modified across the built commits. Renames count in none of the three. | `git diff --name-status` per commit, `-M` to detect renames. |
| `tests_added` / `tests_deleted` | `gh_tests_added` / `gh_tests_deleted` | Test **cases** added/deleted in test files, diffing the previous built commit against the trigger commit. Java: lines matching `@Test` or a `test…(` method signature. | Same regexes. (The audit noted a possible add/delete swap in the original Rugged call; fidelity in M5 checks the sign.) |
| `src_files` / `doc_files` / `other_files` | `gh_src_files` / `gh_doc_files` / `gh_other_files` | Number of touched files by type: programming (source + test), markup/documentation, data/other (no detected language). | Extension table. |
| `commits_on_files_touched` | `gh_commits_on_files_touched` | Number of distinct commits in the 3 months before the build start that touched any file touched by this build, excluding the built commits themselves ("file hotness"). | `git log --since=<start-90d> -- <files>` on a full-history checkout (`fetch-depth: 0`). |
| `team_size` | `gh_team_size` | Distinct developers who committed (non-PR) or merged PRs in the 3 months before the build. TravisTorrent used GHTorrent logins. | Distinct mailmap-normalised author emails in the last 90 days. |
| `by_core_team_member` | `gh_by_core_team_member` | 1 if the build's authors are all in the team above. | Same, by email. |
| `sloc` | `gh_sloc` | Executable lines (comment- and blank-stripped) of the language's files at the trigger commit. | A lightweight per-language counter (Java: strip `//` and `/* */` comments and blank lines). |
| `test_lines_per_kloc` / `test_cases_per_kloc` / `asserts_per_kloc` | `gh_test_lines_per_kloc` / `gh_test_cases_per_kloc` / `gh_asserts_cases_per_kloc` | Test lines / test cases / assert statements per 1,000 `sloc`. (In the 2016 release these are ratios; measured median test lines per kloc on Java: ≈ 342 per build, the grain the model trains on, and ≈ 442 over job rows.) | Same counters. |
| `num_all_built_commits` | `git_num_commits` (approximate; evidence in DATA_DICTIONARY and PIPELINE §8.4: the length of `git_commits`, which never contains the built commit and is offset by one against the previous build's commit) | Number of built commits (§1). | `rev-list` count with the merge-stop rule. |
| `num_commits_in_push` | not present in the 2016 release | — | Not used by the model (NULL in training). |
| `is_pr` | `gh_is_pr` | Pull-request build. | `github.event_name == pull_request`. |
| `description_complexity` | `gh_description_complexity` | PR title + description word count. | **Excluded** (tagged *uncertain*: the stored text may postdate the build). |
| `repo_age_days` | not available in the 2016 release (`gh_first_commit_created_at` is a copy of `tr_started_at` in UK local time, DATA_DICTIONARY → Evidence) | Days since the repository's first commit at build start. | NULL for every training build, so `project_age_days` carries no learned signal. The Action does compute it (`integrations/github-action/extract_change_facts.py`): M4 must drop the feature (and serving must then ignore it) or keep it knowing every training value was missing. |

## 3. Never inputs

The current build's `tr_status`, `tr_duration`, every `tr_*` log/test/latency field, and anything
observed after the build starts (`rl_core.features.schema.FORBIDDEN_INPUTS`). The CI result is
shown **next to** the risk score as a separate signal.
