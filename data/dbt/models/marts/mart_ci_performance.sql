-- Build duration (Travis' summed job time, seconds) percentiles overall, by status and by project.
-- quantile_cont = linear interpolation; the mean is an exact integer sum / count, so the output does
-- not depend on DuckDB's (parallel) summation order. Builds without a duration are excluded.

with d as (
    select project, status, duration_s::bigint as duration_s
    from {{ ref('fct_builds') }}
    where duration_s is not null
),

grouped as (
    select 'overall' as scope, 'all' as scope_key, * exclude (project, status) from d
    union all
    select 'status' as scope, status as scope_key, * exclude (project, status) from d
    union all
    select 'project' as scope, project as scope_key, * exclude (project, status) from d
)

select
    scope,
    scope_key,
    count(*) as n_builds,
    sum(duration_s)::double / count(*) as mean_s,
    quantile_cont(duration_s, 0.50) as p50_s,
    quantile_cont(duration_s, 0.75) as p75_s,
    quantile_cont(duration_s, 0.90) as p90_s,
    quantile_cont(duration_s, 0.95) as p95_s,
    quantile_cont(duration_s, 0.99) as p99_s,
    max(duration_s)::double as max_s
from grouped
group by scope, scope_key
order by scope, scope_key
