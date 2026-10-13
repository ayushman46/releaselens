-- Change-size and test-share profile of each build (pre-build facts of the built commits).
--
-- total_churn = src_churn + test_churn (lines). Buckets (inclusive bounds):
--   0–100 | 101–500 | 501–1,000 | 1,001–5,000 | >5,000
-- test_change_ratio = test_churn / total_churn (NULL when nothing changed). Buckets:
--   no line churn | 0% (source only) | 1–25% | 26–50% | 51–75% | 76–99% | 100% (tests only)
-- (A plain ratio for analytics; rl_core's model feature test_churn_ratio = test / (total + 1).)

with b as (
    select build_id, src_churn, test_churn, files_added, files_deleted, files_modified,
           tests_added, tests_deleted
    from {{ ref('stg_travis_builds') }}
),

profiled as (
    select
        *,
        src_churn + test_churn as total_churn,
        case when src_churn + test_churn > 0
             then test_churn::double / (src_churn + test_churn) end as test_change_ratio,
        files_added + files_deleted + files_modified as files_touched,
        (test_churn > 0 or tests_added > 0 or tests_deleted > 0) as touches_tests
    from b
)

select
    build_id,
    src_churn,
    test_churn,
    total_churn,
    case
        when total_churn is null then 'unknown'
        when total_churn <= 100 then '0–100'
        when total_churn <= 500 then '101–500'
        when total_churn <= 1000 then '501–1,000'
        when total_churn <= 5000 then '1,001–5,000'
        else '>5,000'
    end as churn_bucket,
    case
        when total_churn is null then 99
        when total_churn <= 100 then 1
        when total_churn <= 500 then 2
        when total_churn <= 1000 then 3
        when total_churn <= 5000 then 4
        else 5
    end as churn_bucket_order,
    test_change_ratio,
    case
        when total_churn is null then 'unknown'
        when total_churn = 0 then 'no line churn'
        when test_change_ratio = 0 then '0% (source only)'
        when test_change_ratio <= 0.25 then '1–25%'
        when test_change_ratio <= 0.50 then '26–50%'
        when test_change_ratio <= 0.75 then '51–75%'
        when test_change_ratio < 1 then '76–99%'
        else '100% (tests only)'
    end as test_ratio_bucket,
    case
        when total_churn is null then 99
        when total_churn = 0 then 0
        when test_change_ratio = 0 then 1
        when test_change_ratio <= 0.25 then 2
        when test_change_ratio <= 0.50 then 3
        when test_change_ratio <= 0.75 then 4
        when test_change_ratio < 1 then 5
        else 6
    end as test_ratio_bucket_order,
    files_touched,
    touches_tests
from profiled
order by build_id
