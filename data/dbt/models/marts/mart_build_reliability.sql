-- Project x month (UTC, by build start): volume, failures and failure rate with a 95% Wilson
-- interval. Descriptive only: a month's rate reflects that month's builds, not a cause.

select
    project,
    started_month as month_start,
    count(*) as n_builds,
    count(label) as n_labelled_builds,
    count(*) filter (where label = 1) as n_failures,
    count(*) filter (where status = 'failed') as n_failed,
    count(*) filter (where status = 'errored') as n_errored,
    count(*) filter (where status = 'passed') as n_passed,
    count(*) filter (where status in ('canceled', 'started')) as n_without_outcome,
    case when count(label) > 0
         then count(*) filter (where label = 1)::double / count(label) end as failure_rate,
    {{ wilson_low("count(*) filter (where label = 1)", "count(label)") }} as failure_rate_ci95_low,
    {{ wilson_high("count(*) filter (where label = 1)", "count(label)") }} as failure_rate_ci95_high
from {{ ref('fct_builds') }}
group by project, started_month
order by project, month_start
