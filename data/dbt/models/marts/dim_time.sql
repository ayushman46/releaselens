-- Date spine (UTC days) over the data range: first to last build start date, gap-free.

with bounds as (
    select min(cast(started_at as date)) as first_day, max(cast(started_at as date)) as last_day
    from {{ ref('stg_travis_builds') }}
),

spine as (
    select cast(d as date) as date_day
    from bounds, generate_series(bounds.first_day, bounds.last_day, interval 1 day) as g(d)
)

select
    date_day,
    year(date_day)::integer as year,
    quarter(date_day)::integer as quarter,
    month(date_day)::integer as month,
    cast(date_trunc('month', date_day) as date) as month_start,
    isoyear(date_day)::integer as iso_year,
    week(date_day)::integer as iso_week,
    isodow(date_day)::integer as weekday_iso,
    dayname(date_day) as weekday_name,
    isodow(date_day) >= 6 as is_weekend
from spine
order by date_day
