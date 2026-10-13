# Test fixtures — SYNTHETIC DATA

Both `.csv.gz` files here are **synthetic**. They are used only by the `rl_pipeline` unit tests
and contain **no real TravisTorrent, GitHub or Travis CI data**.

| File | Header | Rows |
|---|---|---|
| `travistorrent_synthetic_fixture.csv.gz` | the real 66-column header of `final-2017-01-25.csv.gz` (secondary dataset) | 30 job rows, 13 builds (java, ruby, python) |
| `travistorrent_5_3_2016_synthetic_fixture.csv.gz` | the real 56-column header of `travistorrent_5_3_2016.csv.gz` (primary dataset, ADR-0017) | 26 job rows, 12 builds (java, ruby) |

Both come from the TravisTorrent Figshare record (DOI 10.6084/m9.figshare.19314170), so schema
validation and the conversion path are exercised exactly as on real data. The rows are made up by
seeded PRNGs in `rl_pipeline.testing.build_rows()` / `build_rows_2016()`. Project names
(`synthetic-org/*`), ids, SHAs, counts and timestamps are fabricated. They include `NA` null
tokens, empty fields, quoted free text with commas (and quotes, 2016), PR and non-PR builds,
multi-job builds and ids of different digit lengths. The 2016 fixture also mimics that release's
quirks: `"true"`/`"false"` booleans, `[id, id]` job lists, `-1` / `-1.0` placeholders,
`tr_prev_build` written as `123.0`, and a job row attached to a second (foreign) build, so
`tr_job_id` is not unique there while `row` is.

Regenerate (byte-stable: fixed seeds, gzip mtime 0):

```sh
uv run python -m rl_pipeline.testing data/pipeline/tests/fixtures
```

`test_fixture_matches_generator` / `test_fixture_2016_matches_generator` fail if a committed file
drifts from its generator.

The 2016 generator also feeds the dbt project's `fixture` target (`make dbt-fixture`,
`rl_pipeline.dbt_runner.prepare_fixture`, CI `data.yml`): it regenerates the same bytes, converts
them with the real converter and runs every dbt model and test on the result (8 Java builds).

Never use these files for analysis, training or any reported number.
