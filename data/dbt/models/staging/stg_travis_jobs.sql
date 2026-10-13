-- One row per Travis job (tr_job_id), job-level columns only.
--
-- Deduplication (measured on the real Java partition, docs/data/DATA_DICTIONARY.md -> Evidence):
-- the raw partition has no two identical rows and no repeated (tr_build_id, tr_job_id) pair; the
-- 21,664 extra rows of duplicated job ids are the SAME job attached to a second build. Per job id
-- we keep the row listed in its own build's tr_jobs (16,520 of the 16,563 duplicated ids have
-- exactly one such row); otherwise the lowest dump row number. build_id is the build of the kept
-- row. Build-level facts live in stg_travis_builds (built from every raw row of a build).
--
-- Placeholders and data errors become NULL (ADR-0017):
--   tr_prev_build = -1 (also written -1.0)  -> travis_prev_build_id NULL. It is job-level (it
--     differs between jobs of one build) and is NOT a usable previous-build link: see
--     int_build_history and docs/data/PIPELINE.md §8 for the measured agreement.
--   negative test counts (17 Java rows, minimum -15) -> NULL.

with ranked as (
    select * from {{ ref('stg_travis_job_rows') }}
)

select
    tr_job_id as job_id,
    tr_build_id as build_id,
    raw_row,
    is_listed_in_build,
    n_rows_for_job,
    gh_project_name as project,
    tr_build_number as build_number,
    case when tr_prev_build > 0 then tr_prev_build end as travis_prev_build_id,
    tr_lan as log_language,
    tr_analyzer as log_analyzer,
    tr_frameworks as test_frameworks,
    tr_setup_time as setup_time_s,
    case when tr_tests_ok >= 0 then tr_tests_ok end as tests_ok,
    case when tr_tests_fail >= 0 then tr_tests_fail end as tests_failed_count,
    case when tr_tests_run >= 0 then tr_tests_run end as tests_run,
    case when tr_tests_skipped >= 0 then tr_tests_skipped end as tests_skipped,
    tr_testduration as test_duration_s,
    tr_purebuildduration as pure_build_duration_s,
    tr_tests_ran as tests_ran,
    tr_tests_failed as tests_failed
from ranked
where job_row_rank = 1
order by job_id
