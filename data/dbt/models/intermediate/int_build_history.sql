-- NAIVE previous-build linkage, for ANALYTICS ONLY (never a model input).
--
-- "Previous" here is simply the project's build that started just before (order: started_at,
-- build_id). It ignores whether that build had FINISHED when this one started, so its outcome
-- may not have been knowable at prediction time. The leakage-safe history (finished strictly
-- before the prediction moment, finish time = upper bound started_at + duration) lives only in
-- rl_core.features.history and reaches the ML marts through ml/mart_build_features (ADR-0006).
-- naive_prev_finished_before_start shows how often the naive link would leak.
--
-- Travis' own tr_prev_build is not used: it is job-level, -1 on 30,966 Java job rows, and (measured
-- in M3a) never equals this started_at predecessor and rarely points into the same project.

with builds as (
    select build_id, project, started_at, finished_at_upper, status, label
    from {{ ref('stg_travis_builds') }}
)

select
    build_id,
    project,
    started_at,
    status,
    label,
    row_number() over w as naive_build_seq,
    lag(build_id) over w as naive_prev_build_id,
    lag(status) over w as naive_prev_status,
    lag(label) over w as naive_prev_label,
    lag(started_at) over w as naive_prev_started_at,
    lag(finished_at_upper) over w as naive_prev_finished_at_upper,
    epoch(started_at - lag(started_at) over w) / 3600.0 as hours_since_naive_prev_start,
    case
        when lag(build_id) over w is null then null
        when lag(finished_at_upper) over w is null then null
        else lag(finished_at_upper) over w < started_at
    end as naive_prev_finished_before_start,
    case
        when lag(status) over w is null then null
        else lag(status) over w || '->' || status
    end as naive_status_transition
from builds
window w as (partition by project order by started_at, build_id)
order by build_id
