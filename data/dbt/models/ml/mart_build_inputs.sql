-- Per-build inputs of rl_core.features.offline.compute_features (ADR-0006): the build timeline
-- (started_at, finished_at = finished_at_upper, failed, duration_s), the label, and every
-- rl_core.features.change.BUILD_INPUT_FIELDS column mapped from the 2016 release. Every build of
-- the Java subset is kept (canceled/started too: they have no label, so rl_core never counts them
-- as history, but they still mark "a build started" for hours_since_last_build); the label filter
-- is applied in mart_build_features.
--
-- Mapping decisions (evidence: docs/data/DATA_DICTIONARY.md, docs/data/PIPELINE.md §8):
--   src_churn <- gh_src_churn, test_churn <- gh_test_churn, files_added/deleted/modified <- gh_*,
--   tests_added/deleted <- gh_tests_* (behave like test-CASE counts), src/doc/other_files <- gh_*
--   (per change), sloc <- gh_sloc, test_lines/test_cases_per_kloc <- gh_*,
--   asserts_per_kloc <- gh_asserts_cases_per_kloc, team_size <- gh_team_size,
--   by_core_team_member <- gh_by_core_team_member, is_pr <- gh_is_pr,
--   commits_on_files_touched <- gh_commits_on_files_touched
--   num_all_built_commits <- git_num_commits: the commits this build newly builds (entries of
--     git_commits, which holds the commits since the previous build's commit, measured off by one:
--     it never contains the built commit, but contains the previous build's commit on 60.9% of
--     builds by project and 72.0% by project and branch)
--   num_commits_in_push <- NULL: no push-level count in this release (GHTorrent push events);
--     ADR-0004 excludes it rather than approximating it differently offline and online
--   description_complexity <- NULL: tagged uncertain (final PR text, may be edited after the
--     build), excluded from model inputs by default
--   repo_age_days <- NULL: gh_first_commit_created_at is a copy of tr_started_at in UK local
--     time (measured on 256,015 of 256,055 job rows), not the repository's first commit

select
    build_id,
    project,
    started_at,
    finished_at_upper as finished_at,
    label as failed,
    duration_s::double as duration_s,
    label,

    src_churn,
    test_churn,
    files_added,
    files_deleted,
    files_modified,
    tests_added,
    tests_deleted,
    src_files,
    doc_files,
    other_files,
    sloc,
    test_lines_per_kloc,
    test_cases_per_kloc,
    asserts_per_kloc,
    null::double as num_commits_in_push,
    num_commits as num_all_built_commits,
    team_size,
    by_core_team_member,
    is_pr,
    null::double as description_complexity,
    commits_on_files_touched,
    null::double as repo_age_days
from {{ ref('stg_travis_builds') }}
order by build_id
