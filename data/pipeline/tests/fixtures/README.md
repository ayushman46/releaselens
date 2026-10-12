# Test fixtures — SYNTHETIC DATA

`travistorrent_synthetic_fixture.csv.gz` is **synthetic**. It is used only by the
`rl_pipeline` unit tests and contains **no real TravisTorrent, GitHub or Travis CI data**.

- Header: the real 66-column header of the TravisTorrent Figshare dump
  (`final-2017-01-25.csv.gz`, DOI 10.6084/m9.figshare.19314170), so schema validation and the
  conversion path are exercised exactly as on real data.
- Rows: 30 job rows (13 builds; java, ruby, python), made up by a seeded PRNG in
  `rl_pipeline.testing.build_rows()`. Project names (`synthetic-org/*`), ids, SHAs, counts and
  timestamps are fabricated. It includes `NA` null tokens, empty fields, quoted free text with
  commas, PR and non-PR builds, multi-job builds and ids of different digit lengths.
- Regenerate (byte-stable: fixed seed, gzip mtime 0):

  ```sh
  uv run python -m rl_pipeline.testing data/pipeline/tests/fixtures/travistorrent_synthetic_fixture.csv.gz
  ```

  `test_fixture_matches_generator` fails if the committed file drifts from the generator.

Never use this file for analysis, training or any reported number.
