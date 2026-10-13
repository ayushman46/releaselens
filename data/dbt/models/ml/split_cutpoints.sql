-- Global chronological split of the LABELLED builds (label not NULL) by started_at:
-- oldest train_fraction (70%) -> train, next validation_fraction (15%) -> validation, newest rest
-- (15%) -> test. The cut points are timestamps: the started_at of the first validation / first
-- test build in (started_at, build_id) order. Builds are assigned by timestamp (train:
-- started_at < validation_start_at; validation: < test_start_at; test: >= test_start_at), so
-- builds sharing a start time never straddle a cut and the realised shares can differ slightly
-- from the targets (reported here). One row. No run_id inside, so identical inputs give
-- identical bytes for every run.

with labelled as (
    select
        build_id,
        started_at,
        row_number() over (order by started_at, build_id) as rn,
        count(*) over () as n
    from {{ ref('mart_build_inputs') }}
    where label is not null
),

targets as (
    select
        max(n) as n_labelled_builds,
        floor(max(n) * {{ var('split_train_fraction') }})::bigint as n_train_target,
        floor(max(n) * ({{ var('split_train_fraction') }} + {{ var('split_validation_fraction') }}))::bigint
            as n_train_validation_target
    from labelled
),

cuts as (
    select
        t.*,
        (select l.started_at from labelled as l where l.rn = t.n_train_target + 1)
            as validation_start_at,
        (select l.started_at from labelled as l where l.rn = t.n_train_validation_target + 1)
            as test_start_at
    from targets as t
)

select
    '{{ var("dataset_version") }}' as dataset_version,
    {{ var('split_train_fraction') }}::double as train_fraction_target,
    {{ var('split_validation_fraction') }}::double as validation_fraction_target,
    c.n_labelled_builds,
    (select min(started_at) from labelled) as first_started_at,
    c.validation_start_at,
    c.test_start_at,
    (select max(started_at) from labelled) as last_started_at,
    (select count(*) from labelled as l
        where c.validation_start_at is null or l.started_at < c.validation_start_at) as n_train,
    (select count(*) from labelled as l
        where l.started_at >= c.validation_start_at
          and (c.test_start_at is null or l.started_at < c.test_start_at)) as n_validation,
    (select count(*) from labelled as l where l.started_at >= c.test_start_at) as n_test
from cuts as c
