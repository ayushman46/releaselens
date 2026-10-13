-- Global chronological split of the LABELLED builds (label not NULL) by started_at:
-- oldest train_fraction (70%) -> train, next validation_fraction (15%) -> validation, newest rest
-- (15%) -> test. The cut points are timestamps: the started_at of the first validation / first
-- test build in (started_at, build_id) order. Builds are assigned by timestamp (train:
-- started_at < validation_start_at; validation: < test_start_at; test: >= test_start_at), so
-- builds sharing a start time never straddle a cut and the realised shares can differ slightly
-- from the targets (reported here). One row. No run_id inside, so identical inputs give
-- identical bytes for every run.
--
-- Label availability at the cuts (reported, not used for the assignment): a build's outcome is
-- observable only once it finished (upper bound finished_at = started_at + duration). The two
-- n_*_not_finished_before_* columns count the labelled builds of a split whose finish is unknown
-- or not before the next split's first start, i.e. outcomes that were not yet known when that
-- split began (M4 may purge them).

with labelled as (
    select
        build_id,
        started_at,
        finished_at,
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
    (select count(*) from labelled as l where l.started_at >= c.test_start_at) as n_test,
    (select count(*) from labelled as l
        where l.started_at < c.validation_start_at
          and (l.finished_at is null or l.finished_at >= c.validation_start_at))
        as n_train_not_finished_before_validation_start,
    (select count(*) from labelled as l
        where l.started_at >= c.validation_start_at and l.started_at < c.test_start_at
          and (l.finished_at is null or l.finished_at >= c.test_start_at))
        as n_validation_not_finished_before_test_start
from cuts as c
