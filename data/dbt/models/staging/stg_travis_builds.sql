-- One row per Travis build (tr_build_id) of the Java subset: 128,819 builds in the real release.
--
-- Build-level columns come from the build's canonical job row (listed in its own tr_jobs first,
-- then the lowest tr_job_id, then the dump row; the same rule as the data dictionary). They are
-- constant across all of a build's job rows; the consistent_within_build tests on
-- stg_travis_job_rows prove it on every run (tr_build_number, tr_num_jobs and git_num_committers
-- are only constant across the build's LISTED rows: foreign rows carry their own build's number,
-- and two shifted rows have cast-failure NULLs).
--
-- Semantics (ADR-0005 / ADR-0017, docs/data/DATA_DICTIONARY.md):
--   status       lower(trim(tr_status)): passed | failed | errored | canceled | started
--   label        1 = failed|errored, 0 = passed, NULL = canceled|started (no observed outcome;
--                excluded from ML)
--   started_at   tr_started_at is UTC wall-clock (measured) -> timestamptz UTC
--   finished_at_upper  started_at + duration_s seconds: an UPPER BOUND of the finish time
--                (tr_duration sums job time, DATASET_AUDIT §4.1); NULL when duration is NULL
--   ci_latency_s tr_ci_latency with -1 (placeholder: 6,756 builds, against 12 at -2 and 22 at 0)
--                and other negative values (13,011 builds, impossible for a latency) -> NULL;
--                ci_latency_quality says which. Post-build: never a model input.
-- Excluded on purpose: gh_first_commit_created_at (a copy of tr_started_at in UK local time, not
-- a commit time), tr_prev_build (job-level and not a previous-build link, see stg_travis_jobs),
-- free text, and the uncertain comment counts / git_merged_with.

with job_rows as (
    select * from {{ ref('stg_travis_job_rows') }}
),

per_build as (
    select
        tr_build_id,
        count(*) as n_job_rows,
        count(*) filter (where is_listed_in_build) as n_listed_job_rows
    from job_rows
    group by tr_build_id
),

canonical as (
    select * from job_rows where build_row_rank = 1
)

select
    c.tr_build_id as build_id,
    c.gh_project_name as project,
    c.gh_lang as language,
    c.git_commit as commit_sha,
    c.git_branch as branch,
    c.gh_is_pr as is_pr,
    c.gh_pull_req_num as pull_request_number,
    c.tr_build_number as build_number,
    c.tr_num_jobs as num_jobs,
    c.tr_jobs_count as num_jobs_listed,
    p.n_job_rows,
    p.n_listed_job_rows,

    lower(trim(c.tr_status)) as status,
    case
        when lower(trim(c.tr_status)) in ('failed', 'errored') then 1
        when lower(trim(c.tr_status)) = 'passed' then 0
    end as label,
    c.tr_duration as duration_s,
    timezone('UTC', c.tr_started_at) as started_at,
    timezone('UTC', c.tr_started_at) + to_seconds(c.tr_duration) as finished_at_upper,
    case when c.tr_ci_latency >= 0 then c.tr_ci_latency end as ci_latency_s,
    case
        when c.tr_ci_latency is null then null
        when c.tr_ci_latency = -1 then 'placeholder'
        when c.tr_ci_latency < 0 then 'negative'
        else 'ok'
    end as ci_latency_quality,

    -- change facts of the built commits (pre-build; 2016 names, rl_core mapping in ml/)
    c.gh_src_churn as src_churn,
    c.gh_test_churn as test_churn,
    c.gh_files_added as files_added,
    c.gh_files_deleted as files_deleted,
    c.gh_files_modified as files_modified,
    c.gh_tests_added as tests_added,
    c.gh_tests_deleted as tests_deleted,
    c.gh_src_files as src_files,
    c.gh_doc_files as doc_files,
    c.gh_other_files as other_files,
    c.gh_commits_on_files_touched as commits_on_files_touched,
    -- codebase state at the built commit
    c.gh_sloc as sloc,
    c.gh_test_lines_per_kloc as test_lines_per_kloc,
    c.gh_test_cases_per_kloc as test_cases_per_kloc,
    c.gh_asserts_cases_per_kloc as asserts_per_kloc,
    -- process
    c.git_num_commits as num_commits,
    c.git_num_committers as num_committers,
    c.gh_team_size as team_size,
    c.gh_by_core_team_member as by_core_team_member,
    -- uncertain (may contain post-build information): kept for analysis, never a model input
    c.gh_description_complexity as description_complexity_uncertain
from canonical as c
inner join per_build as p using (tr_build_id)
order by build_id
