# Dataset audit (M0)

> Every number below was computed from the files on disk on 2026-10-13. The profiling scripts are
> reproduced in M2 as `data/pipeline` code. Unknowns are stated as unknowns.

## 1. What the dataset is

`~/Downloads/travistorrent-java-ci-build-dataset-master.zip` (5,028,159,840 bytes) is the
**raw harvester and log-analyzer output of `TestRoots/travistorrent-tools`**. It is **not** the
56-column TravisTorrent build table (`travistorrent_8_2_2017`). Per project folder (`owner@repo`):

| File | Grain | Columns |
|---|---|---|
| `repo-data-travis.csv` / `.json` | 1 row per **build** | `build_id, commit, pull_req, branch, status, duration, started_at, jobs, event_type` |
| `buildlog-data-travis.csv` / `.json` | 1 row per **job** | `tr_build_id, tr_job_id, tr_build_number, tr_original_commit, tr_log_lan, tr_log_status, tr_log_setup_time, tr_log_analyzer, tr_log_frameworks, tr_log_bool_tests_ran, tr_log_bool_tests_failed, tr_log_num_tests_ok, tr_log_num_tests_failed, tr_log_num_tests_run, tr_log_num_tests_skipped, tr_log_num_test_suites_run, tr_log_num_test_suites_ok, tr_log_num_test_suites_failed, tr_log_tests_failed, tr_log_testduration, tr_log_buildduration` |
| `{build_number}_{build_id}_{sha}_{job_id}.log.bz2` | 1 per job | raw Travis log |
| `errors` (9 projects) | — | Travis API retry messages (HTTP 429, one 503) |

The JSON variants have exactly the same keys and values as the CSVs (0 mismatches across 37
comparable files); only their types differ.

**None of the spec's `gh_*` change, test, codebase or people columns exist**:
`gh_src_churn, gh_test_churn, gh_files_*, gh_tests_*, gh_sloc, gh_*_per_kloc, gh_team_size,
gh_by_core_team_member, gh_num_commits_in_push, gh_commits_on_files_touched, gh_description_complexity`.
In travistorrent-tools they come from a separate enrichment step (`build_data_extraction.rb`,
which needs GHTorrent MySQL/MongoDB). That step was never run for this archive.

## 2. Completeness

- Extracted folder vs zip: 301,941 files and 5,084,740,251 bytes, with **0 missing, 0 extra and
  0 size mismatches**.
- In-repo copy (`data/raw/travistorrent/java-builds`): the first 11 projects alphabetically. DSpace
  is an interrupted prefix (1,193 of 3,818 files, no metadata). Now losslessly APFS-cloned from the
  full copy (see `PROGRESS.md`). The pipeline reads the full extracted folder or the zip.
- Gaps in the source archive itself:
  - **DataDog/dd-agent** has only logs (179,168 files, 7,231 builds by filename) and **no status
    metadata**, so it is unusable for supervised learning.
  - **HubSpot/Singularity** has no buildlog JSON and is missing 18,414 of its 33,318 job logs.

## 3. Real counts (19 projects with metadata)

| Scope | Projects | Builds | Jobs | Failed+errored / non-canceled | PR builds | started_at range (UTC) |
|---|---|---|---|---|---|---|
| All with labels | 19 | **64,736** | 141,103 | **30.20%** (19,463 / 64,452) | 27.62% | 2011-10-12 → 2016-08-31 |
| Java (GitHub primary language) | 12 | **39,728** | 70,871 | **28.77%** (11,394 / 39,608) | 24.97% | 2012-04-16 → 2016-08-31 |
| Java strict (excl. Android app DroidPlanner/Tower) | 11 | 37,560 | 68,703 | 28.73% | 24.18% | — |

Status totals (19 projects): passed 44,989 · failed 11,780 · errored 7,683 · canceled 284.
The brief's "~519K builds" does **not** describe this archive: 64,736 is about 12.5% of it.

### Per project

| Project | Language (GitHub) | Builds | passed / failed / errored / canceled | F+E % | PR % | First → last build |
|---|---|---|---|---|---|---|
| 19wu/19wu | Ruby | 1,022 | 730 / 241 / 51 / 0 | 28.57 | 46.18 | 2012-12 → 2015-05 |
| AlchemyCMS/alchemy_cms | Ruby | 3,059 | 2,269 / 665 / 117 / 8 | 25.63 | 36.97 | 2011-10 → 2016-08 |
| AsyncHttpClient/async-http-client | Java | 452 | 328 / 91 / 26 / 7 | 26.29 | 13.05 | 2016-01 → 2016-08 |
| Atmosphere/atmosphere | Java | 510 | 335 / 85 / 90 / 0 | 34.31 | 9.02 | 2012-05 → 2016-08 |
| BBC-News/wraith (now bbc/wraith) | Ruby | 905 | 699 / 117 / 85 / 4 | 22.42 | 44.86 | 2013-12 → 2016-08 |
| BertrandBordage/django-cachalot | Python | 384 | 207 / 72 / 48 / 57 | 36.70 | 4.95 | 2014-09 → 2016-07 |
| BuildCraft/BuildCraft | Java | 2,518 | 1,263 / 1,179 / 76 / 0 | 49.84 | 23.15 | 2014-05 → 2016-03 |
| CartoDB/cartodb (404 on GitHub) | Ruby/Node | 15,172 | 9,550 / 4,051 / 1,477 / 94 | 36.66 | 28.08 | 2013-03 → 2016-08 |
| CloudSlang/cloud-slang | Java | 3,496 | 2,852 / 323 / 279 / 42 | 17.43 | 32.69 | 2015-01 → 2016-08 |
| CloudifySource/cloudify | Java | 6,065 | 4,418 / 1,028 / 619 / 0 | 27.16 | 2.00 | 2012-05 → 2016-03 |
| DSpace/DSpace | Java | 3,813 | 3,269 / 409 / 134 / 1 | 14.24 | 63.55 | 2013-07 → 2016-08 |
| DataDog/dd-agent | Python | (7,231 by log filename) | no labels | — | — | — |
| DiUS/java-faker | Java | 301 | 227 / 58 / 16 / 0 | 24.58 | 37.87 | 2013-12 → 2016-08 |
| DroidPlanner/Tower | Java (Android) | 2,168 | 1,531 / 562 / 75 / 0 | 29.38 | 38.75 | 2014-06 → 2016-08 |
| FasterXML/jackson-databind | Java | 1,601 | 1,140 / 121 / 340 / 0 | 28.79 | 12.05 | 2014-02 → 2016-08 |
| GeoNode/geonode | Python | 3,848 | 2,905 / 774 / 169 / 0 | 24.51 | 38.64 | 2012-09 → 2016-08 |
| GoClipse/goclipse | Java | 370 | 333 / 33 / 4 / 0 | 10.00 | 3.78 | 2014-07 → 2016-08 |
| Graylog2/graylog2-server | Java | 7,324 | 6,411 / 410 / 476 / 27 | 12.14 | 15.36 | 2012-04 → 2016-08 |
| Guake/guake | Python | 618 | 415 / 174 / 28 / 1 | 32.74 | 29.94 | 2014-02 → 2016-08 |
| HubSpot/Singularity | Java | 11,110 | 6,107 / 1,387 / 3,573 / 43 | 44.82 | 29.35 | 2014-08 → 2016-08 |

## 4. Data-quality findings that shape the design

1. **`duration` is summed job time, not wall-clock.** In 25 of 27 sampled multi-job builds it
   matched the sum of per-job `travis_time` spans. There is **no `finished_at`**. So
   `started_at + duration` is an **upper bound** on finish time. Using it in the spec's
   "finished-before" rule is conservative: leakage-safe, but it discards some legitimately
   finished history.
2. **Builds overlap heavily.** Measured with finish = start + duration, the share of builds
   that start before the previous build finished ranges from 9.42% (Guake) to 81.83% (HubSpot).
   A naive "previous build status" feature would leak labels, which makes the finished-before
   rule essential.
3. **Restarts overwrite `started_at` / `status` / `duration`**: 309 builds start more than 1 h
   after the next-created build (DSpace 113, CartoDB 69). There is no `created_at`.
4. **`tr_log_lan` is unreliable on old logs.** The analyzer falls back to "java" when "java"
   appears 3 or more times, which mislabels GeoNode (Python), CartoDB, 19wu and Guake. Language
   comes from GitHub repo metadata instead.
5. **`tr_build_number` is not unique** (648 shared numbers). Key on `build_id` / `tr_job_id`.
6. **`tr_log_status` is a near-copy of the label**: 89% of failed builds have a "broken" job,
   against 0.2% of passed builds. It is post-build, so it may only be used lagged, for builds that
   had finished.
7. **1,458 commits were built more than once** (3,125 builds), typically a push build and a PR
   build of the same SHA.
8. For PR builds Travis recorded GitHub's **virtual merge commit**. Of 34 sampled, 15 no longer
   resolve on GitHub.

## 5. pre_build vs post_build

| Column(s) | Availability | Use |
|---|---|---|
| `build_id`, `commit`, `pull_req`, `branch`, `event_type`, `jobs` (→ `n_jobs`) | pre_build | identifiers / `is_pr` / matrix size |
| `started_at` | pre_build (= trigger time, modulo restarts) | time features, ordering, history windows |
| `status` | post_build | **label** |
| `duration` | post_build | label-adjacent. Allowed only as a history feature of *finished* earlier builds |
| all `tr_log_*` | post_build | forbidden for the current build. Lagged history only |

## 6. Feature coverage vs spec §8.2

| Spec group | Available from this archive alone? |
|---|---|
| Change size (`src_churn`, `test_churn`, files ±) | ❌ needs git enrichment (ADR-0004) |
| Tests in change (`tests_added/deleted`) | ❌ needs git enrichment |
| Codebase (`sloc`, test density) | ❌ needs git enrichment |
| Process/people (`team_size`, core member, `commits_on_files_touched`) | ❌ git enrichment (team size is our own git-only definition) |
| `is_pr` | ✅ |
| `num_commits_in_push` | ❌ not derivable from git; proxy = commits since previous built commit |
| `description_complexity` | ❌ needs the PR API, so it is dropped (ADR-0004) |
| History (`prev_build_failed`, fail rates, …) | ✅ with the conservative finished-before rule |
| Time (hour-of-week) | ✅ (UTC only) |

## 7. The training source: Figshare TravisTorrent (ADR-0004)

`final-2017-01-25.csv.gz` (DOI 10.6084/m9.figshare.19314170, CC BY 4.0), 264,607,174 bytes,
md5 `5d359345fecf0bf47be6edb10b5aae58` (verified locally). Streamed with DuckDB 1.5.6, never
decompressed to disk:

| Measure | Value |
|---|---|
| Columns | 66 (2017 names: `git_diff_src_churn`, `gh_diff_files_added`, … plus `gh_repo_age`, `gh_repo_num_commits`) |
| Job rows | 3,881,992 |
| Builds (distinct `tr_build_id`) | 925,897 |
| Projects | 948 |
| Java | 401,767 jobs · **200,561 builds** · 241 projects |
| Ruby | 1,703,900 jobs · 273,220 builds · 248 projects |
| Python | 1,520,967 jobs · 338,176 builds · 261 projects |
| Go | 255,358 jobs · 113,940 builds · 198 projects |

The full per-column profile (null rates, pre/post-build tags) of this release is
`docs/data/DATA_DICTIONARY_final_2017.md`, generated by `rl_pipeline` (dataset key
`travistorrent-final-2017-01-25`). `docs/data/DATA_DICTIONARY.md` now profiles the primary
release (§9).

## 8. Change-column quality check → training source switched (ADR-0017)

Profiled on Java builds (one row per build):

| Column (2017 name / 2016 name) | `final-2017-01-25` zero % (200,561 builds) | `5_3_2016` zero % (128,819 builds) |
|---|---|---|
| `git_diff_src_churn` / `gh_src_churn` | **99.08** | 32.21 |
| `git_diff_test_churn` / `gh_test_churn` | **100.00** | 70.69 |
| `gh_diff_src_files` / `gh_src_files` | **99.06** | 26.56 |
| `gh_diff_doc_files` / `gh_doc_files` | 100.00 | 94.43 |
| `gh_diff_other_files` / `gh_other_files` | 0.45 | 60.58 |
| `gh_diff_files_modified` / `gh_files_modified` | 2.53 | 2.79 |
| `gh_diff_tests_added` / `gh_tests_added` | 98.50 | 97.95 |

The 2017 re-publication classified almost every file as "other", so line churn was never counted.
The 2016 release (`travistorrent_5_3_2016.csv.gz`, 81,902,127 bytes,
md5 `4713d8b73b667a2c375b89c20f531fc0`, 56 columns with the spec's names) is used for training
and analytics:

- 128,819 Java builds and 402 projects; `tr_started_at` 2011-06-20 → 2015-08-19.
- Statuses: passed 91,282 · failed 22,068 · errored 15,267 · canceled 199 · started 3.
- Duplicates: 256,055 job rows but 234,391 distinct job ids, deduplicated in staging.
- `gh_src_churn` quantiles: p50 9, p90 320, p99 3,669 lines.
- 51,666 Java builds (87 projects) appear in both releases, which enables a status cross-check.

## 9. M2b: the 5_3_2016 release in the pipeline (measured 2026-10-13)

Converted with `rl_pipeline` (dataset key `travistorrent-5-3-2016`, version
`4713d8b73b667a2c375b89c20f531fc0`); the generated `DATA_DICTIONARY.md` has every figure and the
queries' definitions. Highlights:

- 2,640,825 job rows, 56 columns, two languages: Java 256,055 rows / 234,391 distinct job ids /
  128,819 builds / 402 projects; Ruby 2,384,770 / 1,565,999 / 231,951 / 898.
- **`tr_job_id` is not unique** (1,800,390 distinct over all rows) while the dump's `row` is.
  In Java, all 16,563 duplicated job ids occur under more than one `tr_build_id`, and for 16,520 of
  them exactly one row lists the job in its own build's `tr_jobs`: the duplicates are job rows
  attached to a second build, not repeated copies. Staging should keep the listed row.
- **12 rows (2 Java) have shifted trailing fields** (a `tr_failed_tests` value with unescaped quotes
  and commas). They are the only rows with cast failures (76 values in 7 columns); raw keeps them.
- **`tr_started_at` is UTC**: equal to the monperrus archive's explicit-UTC `started_at` for all
  7,317 joined Java builds (status equal on 7,317, duration on 7,315).
- **`gh_first_commit_created_at` is not a commit time**: it equals `tr_started_at` rendered in
  Europe/London local time on 256,015 of 256,055 Java rows (offset exactly 0 h, +1 h or +2 h on
  every row).
- Placeholders: `tr_prev_build` = -1 on 30,966 Java job rows; `tr_ci_latency` = -1 on 6,756 Java
  builds and below -1 on 13,011 more. `tr_prev_build >= tr_build_id` on 81,249 of 225,087 Java job
  rows with a positive link, and the link differs between jobs of one build in 16,169 builds, so
  history must be ordered by `tr_started_at`, not by this link.
- Release cross-check: 51,666 Java build ids appear in both releases — ADR-0017 says 87 projects,
  this measurement finds 90 distinct project names (same count with either release's names).
  `tr_status` agrees on 51,663.


## 10. M3a: facts the dbt staging relies on (measured 2026-10-13)

Measured on the Java partition of the primary release (one canonical row per build: listed in its
own `tr_jobs` first, then the lowest `tr_job_id`, `row`); details and queries' definitions in
`docs/data/PIPELINE.md` §8.

- No two raw rows are identical apart from `row`, and every `(tr_build_id, tr_job_id)` pair is
  unique (256,055 pairs): the duplicate job ids are foreign attachments, not copies.
- **`tr_prev_build` does not link to the previous build.** Among 112,914 builds with a positive
  value, the id is a Java build of this release for 11,513, a build of the same project for 49,
  and equal to the project's previous build by `tr_started_at` for 0 (of 112,570 with a
  predecessor). −1 appears on 15,905 builds, only 58 of them the project's first build.
- **`git_commits` is the commit range since the previous build, offset by one.** It never contains
  the built commit, contains the project's previous build's commit on 78,156 of 128,417 builds
  (60.9%; 72.0% = 85,603 of 118,880 when the previous build is taken on the same branch), and a
  build's own commit appears in another build's `git_commits` for 94,560 of 128,819 builds. Its
  length (`git_num_commits`) is therefore used as rl_core's `num_all_built_commits`.
- **`tr_ci_latency = -1` is a placeholder** (6,756 builds, against 12 at −2 and 22 at 0); 13,011
  builds have impossible values below −1. Both become NULL in staging.
- With the naive "previous build by start time", the predecessor's upper-bound finish
  (`tr_started_at + tr_duration`) is not before the build's start for 28,211 of 128,417 builds
  (21.97%): history features must use rl_core's finished-before rule.
