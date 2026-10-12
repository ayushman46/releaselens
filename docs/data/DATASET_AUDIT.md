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

The full per-column profile (null rates, pre/post-build tags) is `docs/data/DATA_DICTIONARY.md`,
generated in M2 by `rl_pipeline`.
