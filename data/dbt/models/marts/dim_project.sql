-- One row per project: language, observed build window, volume and failure rate.
-- failure_rate = builds with label 1 / labelled builds (canceled and started excluded).

select
    project,
    any_value(language) as language,
    min(started_at) as first_build_at,
    max(started_at) as last_build_at,
    count(*) as n_builds,
    count(label) as n_labelled_builds,
    count(*) filter (where label = 1) as n_failures,
    count(*) filter (where status = 'passed') as n_passed,
    count(*) filter (where status = 'failed') as n_failed,
    count(*) filter (where status = 'errored') as n_errored,
    count(*) filter (where status = 'canceled') as n_canceled,
    count(*) filter (where status = 'started') as n_started,
    case when count(label) > 0
         then count(*) filter (where label = 1)::double / count(label) end as failure_rate,
    {{ wilson_low("count(*) filter (where label = 1)", "count(label)") }} as failure_rate_ci95_low,
    {{ wilson_high("count(*) filter (where label = 1)", "count(label)") }} as failure_rate_ci95_high
from {{ ref('stg_travis_builds') }}
group by project
order by project
