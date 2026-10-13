-- One row per Java build: identity, outcome, timing and change profile (analytics fact table).
-- label: 1 failed|errored, 0 passed, NULL canceled|started (ADR-0005/0017).

select
    b.build_id,
    b.project,
    b.language,
    b.started_at,
    t.started_date,
    t.started_month,
    b.finished_at_upper,
    b.status,
    b.label,
    b.duration_s,
    b.is_pr,
    b.by_core_team_member,
    b.team_size,
    b.num_jobs,
    b.num_commits,
    c.src_churn,
    c.test_churn,
    c.total_churn,
    c.churn_bucket,
    c.churn_bucket_order,
    c.test_change_ratio,
    c.test_ratio_bucket,
    c.test_ratio_bucket_order,
    c.files_touched,
    c.touches_tests,
    t.hour_utc,
    t.weekday_iso,
    t.is_weekend
from {{ ref('stg_travis_builds') }} as b
inner join {{ ref('int_change_profile') }} as c using (build_id)
inner join {{ ref('int_build_timing') }} as t using (build_id)
order by b.build_id
