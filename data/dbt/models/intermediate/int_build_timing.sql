-- When builds start (UTC; the session TimeZone is pinned to UTC in profiles.yml) and how long
-- they take. duration_s is Travis' summed job time (DATASET_AUDIT §4.1), not wall-clock.

select
    build_id,
    started_at,
    finished_at_upper,
    cast(started_at as date) as started_date,
    cast(date_trunc('month', started_at) as date) as started_month,
    extract(hour from started_at)::integer as hour_utc,
    isodow(started_at)::integer as weekday_iso,
    dayname(started_at) as weekday_name,
    isodow(started_at) >= 6 as is_weekend,
    duration_s,
    duration_s / 60.0 as duration_min
from {{ ref('stg_travis_builds') }}
order by build_id
