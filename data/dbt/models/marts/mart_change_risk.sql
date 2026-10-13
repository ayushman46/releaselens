-- Failure rate by change-size bucket and by test-share bucket (one row per dimension x bucket),
-- with 95% Wilson intervals. Associations only: bigger changes may fail more often for many
-- reasons (project, author, period); nothing here is a causal effect.

with dims as (
    select 'churn_bucket' as dimension, churn_bucket as bucket, churn_bucket_order as bucket_order,
           label, status
    from {{ ref('fct_builds') }}
    union all
    select 'test_ratio_bucket' as dimension, test_ratio_bucket as bucket,
           test_ratio_bucket_order as bucket_order, label, status
    from {{ ref('fct_builds') }}
)

select
    dimension,
    bucket,
    bucket_order,
    count(*) as n_builds,
    count(label) as n_labelled_builds,
    count(*) filter (where label = 1) as n_failures,
    count(*) filter (where status = 'errored') as n_errored,
    case when count(label) > 0
         then count(*) filter (where label = 1)::double / count(label) end as failure_rate,
    {{ wilson_low("count(*) filter (where label = 1)", "count(label)") }} as failure_rate_ci95_low,
    {{ wilson_high("count(*) filter (where label = 1)", "count(label)") }} as failure_rate_ci95_high
from dims
group by dimension, bucket, bucket_order
order by dimension, bucket_order
