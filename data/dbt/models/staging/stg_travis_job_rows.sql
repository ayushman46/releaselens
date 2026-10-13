{{ config(materialized='view') }}

-- Every raw Java job row, typed as converted (no deduplication), with the two facts staging needs
-- to pick canonical rows (docs/data/DATA_DICTIONARY.md -> Evidence, "duplicate job rows"):
--   * is_listed_in_build: the row's tr_job_id appears in its own build's tr_jobs list. Rows that
--     are not listed are job rows attached to a second (foreign) build: their job-level columns
--     (and tr_build_number) belong to the job's real build, their build-level columns to tr_build_id.
--   * build_row_rank / job_row_rank: deterministic preference orders used by stg_travis_builds and
--     stg_travis_jobs (listed rows first, then the lowest tr_job_id / dump row number).
-- A view: the S3 object is read where it is used, nothing is copied into the DuckDB file.

with src as (
    select * from {{ source('travistorrent', 'java_jobs') }}
),

parsed as (
    select
        *,
        list_transform(
            string_split(trim(tr_jobs, '[] '), ','),
            x -> try_cast(trim(x) as bigint)
        ) as tr_jobs_ids
    from src
)

select
    row as raw_row,
    tr_build_id,
    tr_job_id,
    coalesce(list_contains(tr_jobs_ids, tr_job_id), false) as is_listed_in_build,
    row_number() over (
        partition by tr_build_id
        order by coalesce(list_contains(tr_jobs_ids, tr_job_id), false) desc, tr_job_id, row
    ) as build_row_rank,
    row_number() over (
        partition by tr_job_id
        order by coalesce(list_contains(tr_jobs_ids, tr_job_id), false) desc, row
    ) as job_row_rank,
    count(*) over (partition by tr_job_id) as n_rows_for_job,
    len(tr_jobs_ids) as tr_jobs_count,
    * exclude (row, tr_build_id, tr_job_id, tr_jobs_ids)
from parsed
