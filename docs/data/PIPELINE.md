# Historical pipeline (M2/M2b/M3a): TravisTorrent → Parquet → S3 → dbt marts + features

> Orchestration for `historical_pipeline` (spec §7.2, ADR-0004, ADR-0006, ADR-0009, ADR-0013,
> ADR-0017). The task code is `rl_pipeline` (`data/pipeline`, Airflow-agnostic) and the dbt-duckdb
> project `data/dbt` (§8). Airflow only wires them together. Every number in §6 and §8 was measured
> on the dev machine on 2026-10-13 (Colima: 2 CPU / 4 GiB) with the commands shown.

## 0. Datasets (registry, M2b)

`rl_pipeline.datasets` registers every release the pipeline can ingest. A `DatasetSpec` holds the
Figshare source (file, URL, size, md5), the download sub-directory, the raw schema with per-column
cast patterns, the conversion keys, timestamp semantics and the column catalog.

| Key | Role | File · bytes · md5 | Download sub-dir | Columns | Sort key (unique column last) | Dictionary |
|---|---|---|---|---:|---|---|
| `travistorrent-5-3-2016` | **primary, default** (ADR-0017) | `travistorrent_5_3_2016.csv.gz` · 81,902,127 · `4713d8b7…` | `travistorrent-figshare-19314170-5_3_2016` | 56 | `tr_build_id, tr_job_id, row` | `DATA_DICTIONARY.md` |
| `travistorrent-final-2017-01-25` | secondary (status cross-check) | `final-2017-01-25.csv.gz` · 264,607,174 · `5d359345…` | `travistorrent-figshare-19314170` | 66 | `tr_build_id, tr_job_id` | `DATA_DICTIONARY_final_2017.md` |

- Every task, the CLI (`python -m rl_pipeline <task> --dataset <key>`) and the DAG (`params.dataset`)
  take the key; the default is the primary. `python -m rl_pipeline list_datasets` prints the registry.
  Task results carry `"dataset"` forward. Without a key, a `version` equal to a registered md5
  selects that release.
- Both releases land in the same layout, `raw/travistorrent/version=<md5>/lang=<lang>/part-00000.parquet`
  plus `_manifest.json`, locally under `RL_LAKE_DIR` and in S3. The md5s differ, so the prefixes
  never collide.
- Raw stays faithful: no deduplication, no placeholder remapping. Only `NA` and empty fields become
  NULL; `-1` stays `-1` (staging decides). The 2016 release needs one pattern override:
  `tr_prev_build` is written both as `123` and `123.0` (`-1.0`), so it accepts an all-zero
  fraction (exact values only; `12.5` is still a cast failure).
- The 2016 release's `tr_job_id` is not unique (job rows attached to more than one build, see
  `DATA_DICTIONARY.md` → Evidence), so its gate requires the dump's `row` number to be unique and
  non-null instead, and `row` completes the sort order. The 2017 gate is unchanged (unique
  `tr_job_id`).
- The secondary spec reproduces the pre-registry converter exactly: same converter block (schema
  fingerprint `5dd3f6d6…`) and manifest fields (`manifest_extras=False`), so its published version
  stays byte-identical (proved in §6.1). The primary's manifest adds `dataset_key`,
  `column_patterns` and `key_profile`.

## 1. How to run

| Command | What it does |
|---|---|
| `make up-data` | Builds `rlens/python` → `rlens/airflow` (in that order), starts `postgres` + `minio` (pgsty/silo) and waits until healthy, then runs `minio-init` (creates `releaselens-data` and `releaselens-mlflow`, idempotent). |
| `make pipeline` | `up-data`, then **`airflow dags test historical_pipeline`** in a one-shot `airflow-cli` container (`compose run --rm`) for the default (primary) dataset. Its dependencies `minio-init` and `airflow-init` (`airflow db migrate`) run first. The exit code is non-zero when the DAG run fails. |
| `make pipeline DATASET=travistorrent-final-2017-01-25` | Same for another registered dataset (sets `params.dataset`). |
| `make pipeline CONF='{"skip_training": true}'` | Same, passing DAG params (`--conf`). `DATASET` and `CONF` are merged; a `CONF` whose `"dataset"` contradicts `DATASET` is an error. |
| `make pipeline-test-dag` | DAG checks without a DB or S3. Builds the `dag-test` target (Airflow image + pytest + mypy, never shipped) and runs `mypy` (with Airflow's own types) and 37 pytest DAG-integrity tests (including: the DAG's dataset enum equals the registry in the workspace venv). CI runs the same target build in `backend.yml` (`images` job). In the workspace venv (`make test`, CI unit jobs) the DAG test module is reported as skipped, because Airflow is not installed there. |
| `make up-airflow` | The full Airflow 3 deployment: `airflow-api-server` (UI/API on <http://localhost:28080>), `airflow-scheduler` (LocalExecutor), `airflow-dag-processor` and `airflow-triggerer`. Profile `airflow-full`. See §5 before using it on a 4 GiB VM. |
| `make stop-data` | Stops `minio` and the four Airflow services. Core services keep running. |
| `make ps` / `logs` / `down` | Now include the `data` and `airflow-full` profiles. |
| `make test` / `typecheck` / `test-integration` | Also cover `rl_pipeline` (`data/pipeline`), including the dbt fixture build (§8.6). |
| `make dbt-build DBT_RUN_ID=<id>` | M3a: `dbt deps` + the four dbt stages on the **host** (workspace venv) against the object store on `localhost:29000` (§8). Same functions as the DAG tasks. |
| `make dbt-fixture` | M3a: the whole dbt project on the SYNTHETIC fixture, local Parquet only (no S3); what CI runs. |

`make doctor` (a prerequisite of `up-data`) refuses to run when the host has less than
`MIN_FREE_GB` (default 2) GiB free. The data itself lives in Docker volumes inside the Colima VM.

### DAG params

| Param | Default | Meaning |
|---|---|---|
| `dataset` | `travistorrent-5-3-2016` | Registered dataset key (enum of the registry, validated by Airflow). |
| `skip_training` | `true` | Short-circuits `train_models` / `evaluate_and_register` (default `true` until M4). |
| `dataset_dir` | `null` | Overrides `<RL_DATASETS_DIR>/<dataset sub-dir>` (the directory that holds the dataset's `.csv.gz`) for this run. |
| `allow_download` | `true` | When the file is missing or corrupt: download it from Figshare (`true`) or fail (`false`). The compose mount of the downloads dir is read-only, so in that case the download goes to `/lake/downloads/<dataset sub-dir>/` (see §2). |

## 2. Tasks

```
download_or_locate_dataset → validate_raw_schema → convert_to_parquet → load_raw_s3
  → dbt_deps → dbt_build_staging → dbt_build_intermediate → dbt_build_marts      (M3a, dbt)
  → export_analytics_to_postgres                                     (M3b placeholder)
  → build_feature_snapshot                                           (M3a, dbt ml/ models)
  → training_enabled [short-circuit on params.skip_training]
  → train_models → evaluate_and_register                             (M4 placeholders)
```
`convert_to_parquet` consumes the located-dataset dict but also depends on `validate_raw_schema`, so it
never starts before the schema check has passed.

| Task | Runs in | Timeout | What it does |
|---|---|---|---|
| `download_or_locate_dataset` | workspace venv | 30 min | `rl_pipeline.tasks.locate_or_download`: finds the dataset's file in `<RL_DATASETS_DIR>/<sub-dir>` and verifies the size plus the publisher's md5 from the spec. It downloads (resumable, md5-verified, `testroots.org` refused) only when the file is missing or corrupt. Returns `{dataset, path, md5, size, version=md5}`. If the dataset dir is not writable (the compose `:ro` mount) and holds no verified copy, it logs a warning and downloads to `<RL_LAKE_DIR>/downloads/<sub-dir>/` instead; the read-only copy is never touched. |
| `validate_raw_schema` | workspace venv | 10 min | Header must equal the spec's column names, in order (56 for the primary, 66 for the secondary). Raises on mismatch. |
| `convert_to_parquet` | workspace venv | 45 min | DuckDB streams the `.csv.gz` (never decompressed to disk). A data-quality gate runs first: per-column cast-failure rate ≤ 0.1 %, the spec's unique column unique and non-null (`row` / `tr_job_id`), non-null `tr_job_id` and `tr_build_id`, path-safe `gh_lang`. Then one typed, sorted, ZSTD Parquet file per language goes to `/lake/raw/travistorrent/version=<md5>/`, with `_manifest.json` written last. |
| `load_raw_s3` | workspace venv | 30 min | Uploads to `s3://releaselens-data/raw/travistorrent/version=<md5>/lang=<lang>/part-00000.parquet`, plus `_manifest.json` last. Verifies the listing sizes afterwards. |
| `dbt_deps`, `dbt_build_staging`, `dbt_build_intermediate`, `dbt_build_marts`, `build_feature_snapshot` | workspace venv | 10 / 30 / 30 / 30 / 30 min | `rl_pipeline.tasks.<same name>` → `rl_pipeline.dbt_runner` (dbtRunner, §8). `dbt_deps` starts the context from `load_raw_s3`'s dict (dataset, version) and the Airflow `run_id` (`{{ run_id }}`, sanitised to a path-safe segment); every later task gets the previous task's dict. A failing model or dbt test fails the task. For a dataset without dbt models (the secondary) the body exits with code 99 and Airflow records **skipped** (`skip_on_exit_code=99`). `build_feature_snapshot` follows the export placeholder, so it uses `trigger_rule=none_failed`. |
| `export_analytics_to_postgres` (M3b), 2 × M4 placeholders | Airflow venv | 1 min | Raise `AirflowSkipException("<task>: implemented in M3b/M4 …")`. They are **skipped, never a fake success**. They use `trigger_rule=none_failed`, so each one runs and logs its own reason; an upstream failure still marks them `upstream_failed`. |
| `training_enabled` | Airflow venv | 1 min | `@task.short_circuit`: returns `not params.skip_training`. |

**Isolation (ADR-0013).** Airflow 3.3.2 lives in `/opt/airflow-venv` (official constraints), and
`rl_pipeline` lives in the workspace venv `/app/.venv`. Implemented tasks are
`@task.external_python(python="/app/.venv/bin/python", expect_airflow=False)`. Their bodies only
import `rl_pipeline.tasks` and pass XCom dicts, which are JSON-serialisable and are pickled across
the interpreter boundary. Each body also calls `rl_pipeline.__main__.configure_cli_logging()`, so
rl_pipeline's JSON log lines appear in the Airflow task log.

**Retries.** `retries=2`, `retry_delay=30s`, `retry_exponential_backoff=2.0` (Airflow ≥ 3.3 takes a
float multiplier, and 2.0 is what the legacy `True` meant), and `max_retry_delay=5min`.
`max_active_runs=1`, `dagrun_timeout=3h`. Data-quality errors are **not caught**: the task fails
after its retries, and the run fails.

## 3. Idempotency

1. **Version = md5 of the source file**, re-verified on every run. Every path and S3 key carries `version=<md5>`; each registered dataset has its own md5, hence its own prefix.
2. **Conversion skip:** `convert_to_parquet` returns immediately (`"conversion skipped"`) when
   `/lake/.../version=<md5>/_manifest.json` exists for the same converter settings and schema
   fingerprint **and** every file's sha256 still matches.
3. **Upload skip:** every object carries `x-amz-meta-sha256`. An upload is skipped when the key
   already holds the same sha256. `_manifest.json` is written last, so its presence means the
   version is complete.
4. **Deterministic output:** rows are ordered by the spec's sort key (`(tr_build_id, tr_job_id)`;
   2016: `(tr_build_id, tr_job_id, row)`), a total order, with a fixed row-group size and ZSTD.
   Measured: a from-scratch re-conversion in the container (fresh `/lake` volume) produced
   **byte-identical** Parquet (same sha256 for all 4 files, and also identical to a conversion run
   on the macOS host). S3 then skipped all 5 uploads. M2b: the 2016 conversion is byte-identical
   between the macOS host and the container as well (both Parquet files and the manifest, §6.1).

## 4. Compose services (`infra/compose/compose.yaml`)

| Service | Profile | Image | Notes |
|---|---|---|---|
| `minio` | data | `pgsty/silo:RELEASE.2026-09-16T00-00-00Z` | Ports 29000 (S3) / 29001 (console), bound to `127.0.0.1` only (the root credentials are the public dev defaults). Volume `minio-data`. Healthcheck: `curl -f /minio/health/live` (the image ships curl and mc, but not wget or nc). `mem_limit 256m`. |
| `minio-init` | data | `rlens/python:dev` | `python -m rl_pipeline.s3 ensure-buckets` (one-shot, idempotent). |
| `airflow-init` | data | `rlens/airflow:dev` | `ensure_metadata_db.py` creates the `airflow` DB only if it is missing (the postgres init script only runs on a fresh volume), then `airflow db migrate`. Creates nothing else. |
| `airflow-cli` | data | `rlens/airflow:dev` | One-shot CLI used by `make pipeline`. `mem_limit 2g`, because the whole DAG, including DuckDB, runs inside it. |
| `airflow-api-server`, `-scheduler`, `-dag-processor`, `-triggerer` | **airflow-full only** | `rlens/airflow:dev` | LocalExecutor, so tasks run in the scheduler container (`mem_limit 2g`). They share a JWT secret and the `airflow-logs` volume. The UI runs without a login (`SIMPLE_AUTH_MANAGER_ALL_ADMINS`, dev only), so port 28080 is bound to `127.0.0.1` only. |

Mounts for every Airflow container:
- host downloads dir (`RL_DATASETS_HOST_DIR`, default `data/lake/downloads`, one sub-directory per
  registered dataset) → `/datasets:ro` (`RL_DATASETS_DIR=/datasets`); each task resolves
  `/datasets/<dataset sub-dir>`. The former single-dataset variables `RL_DATASET_DIR` /
  `RL_DATASET_PATH` are rejected with an explanatory error (a stale value would point at the wrong
  file);
- volume `lake` → `/lake` (`RL_LAKE_DIR`, the Parquet staging area plus DuckDB spill);
- `data/airflow/dags` → `/opt/airflow/dags:ro` (DAG edits need no rebuild);
- `data/dbt` → `/opt/airflow/dbt:ro` (`RL_DBT_PROJECT_DIR`, also baked into the image; model edits
  need no rebuild). dbt's DuckDB file, `target/`, `logs/` and spill go to `/lake/dbt`.

`RL_DUCKDB_MEMORY_LIMIT=1GB` keeps DuckDB well inside the container limit.
Metadata DB: `postgresql+psycopg2://…@postgres:5432/airflow`. `psycopg2-binary` and `asyncpg` come
from Airflow's constraints, so the Postgres provider is not installed. All values are overridable
in `infra/compose/.env` (see `.env.example`).

## 5. `airflow dags test` vs the full deployment

Airflow 3's docs recommend at least 4 GB RAM for the four-process deployment, and the Colima VM has
4 GiB total, shared with the core stack and other projects. So the **verification path on this
machine is `make pipeline`**: `airflow dags test` runs the real DAG, real task code, real retries,
and real XComs against the real metadata DB, in a single process (ADR-0013). The full deployment is
defined, and was smoke-tested once (§6). It sits in its own `airflow-full` profile, so
`--profile data` never starts it by accident. For regular use, give Docker ≥ 8 GiB
(`colima stop && colima start --cpu 6 --memory 10`, ADR-0015).

## 6. Measured (2026-10-13, Colima 2 CPU / 4 GiB, core stack running alongside)

### 6.1 M2b: primary dataset `travistorrent-5-3-2016` (registry refactor)

Measured after the registry refactor. Timings come from the Airflow metadata DB (`dag_run`,
`task_instance`) and from `date` around `make pipeline`; object listings from `list_objects_v2` +
`head_object`. `make doctor` was run with `MIN_FREE_GB=1` (the host had 1.9–2.0 GiB free, below the
2 GiB default; the data lives in the Colima VM).

| Run | Command | `make` wall | DAG run | locate / validate / convert / load | Result |
|---|---|---|---|---|---|
| 1 (cold for this version) | `make pipeline` | 31 s | 23.02 s | 2.16 / 0.62 / 18.26 / 1.20 s | converted (profile 6.4 s + write 11.0 s), `uploaded=3, skipped=0` |
| 2 (repeat) | `make pipeline` | 14 s | 5.02 s | 2.14 / 0.63 / 0.73 / 0.71 s | `conversion skipped` (manifest and all checksums match), `uploaded=0, skipped=3` |
| secondary | `make pipeline DATASET=travistorrent-final-2017-01-25` | 13 s | 5.62 s | — | its pre-registry `/lake` conversion is accepted (`conversion skipped`), `uploaded=0, skipped=5` |

**Object listing** (`s3://releaselens-data/raw/travistorrent/version=4713d8b73b667a2c375b89c20f531fc0/`),
**identical after runs 1 and 2 and after the secondary run** (key, size, ETag, metadata sha256 and
LastModified; the `version=5d359345…` objects were not rewritten either). 82,291,046 bytes, 2,640,825 rows:

| Key | Rows | Bytes | sha256 (prefix) |
|---|---:|---:|---|
| `lang=java/part-00000.parquet` | 256,055 | 18,385,502 | `ff8cc162ddc963d9` |
| `lang=ruby/part-00000.parquet` | 2,384,770 | 63,890,601 | `a8cb262947d4567b` |
| `_manifest.json` | — | 14,943 | `59544c061ed5c9aa` |

- Parquet/input ratio 82,276,103 / 81,902,127 = 1.005 (the spec's disk estimate uses 1.25).
- Cast failures: 76 values in 7 columns, all on the 12 rows with shifted trailing fields (2 Java);
  max rate 0.0044 % (`tr_purebuildduration`, 12 of 273,377), far below the 0.1 % gate.
- Key profile: `row` unique and non-null; 840,435 duplicate `tr_job_id` rows (1,800,390 distinct
  job ids in 2,640,825 rows), kept in raw.
- Host CLI chain (`locate_or_download → validate_raw_schema → convert_to_parquet`, macOS,
  `RL_DUCKDB_MEMORY_LIMIT=3GB`): conversion 11.85 s, peak RSS 3.63 GB (`/usr/bin/time -l`); the
  same sha256 for both Parquet files and the manifest as the container run (1 GB DuckDB limit).

**Secondary dataset unchanged (byte-identity proof).** `final-2017-01-25.csv.gz` re-converted with
the refactored code into a scratch lake on the host (CLI chain with `--dataset
travistorrent-final-2017-01-25`, 34.4 s): all four Parquet files have exactly the sha256 values of
the published objects (`dc7fa347…4f`, `43ab8396…14`, `ee5dbff5…7a`, `750d0a23…57`, the prefixes in
§6.2), and `_manifest.json` is byte-identical to the published one (sha256 `0ae40235…21e0`,
`cmp` against the S3 copy). The scratch output was deleted afterwards.

**Release cross-check.** 51,666 Java build ids are in both releases (90 distinct project names in
either release). `tr_status` agrees on 51,663 (2 `started` → `canceled`, 1 `passed` → `failed`),
start time on 51,662 and `tr_duration` on 51,660.

### 6.2 M2: `final-2017-01-25` (original measurements)

Official runs started from an empty S3 prefix and a fresh `lake` volume. Timings come from the
Airflow metadata DB (`dag_run`, `task_instance`) and from `date` around `make pipeline`. Memory
comes from `docker stats` sampled every ~2 s plus cgroup v2 `memory.stat:anon`/`memory.peak`/
`memory.events:oom_kill`, read every 1 s inside the VM.

| Run | `make pipeline` wall | DAG run | convert | load | Result |
|---|---|---|---|---|---|
| 1 (cold) | 110 s | 73.8 s | 66.6 s | 2.2 s | 4 Parquet + manifest uploaded (`uploaded=5, skipped=0`) |
| 2 (repeat) | 40 s | 5.4 s | 1.0 s (skipped) | 0.8 s | `conversion skipped`, `uploaded=0, skipped=5` |
| 3 (fresh `/lake`, S3 kept) | 89 s | 54.4 s | re-converted | — | byte-identical Parquet → `uploaded=0, skipped=5` |
| failure test¹ | 189 s | 155.3 s | — | — | `download_or_locate_dataset` failed after 3 tries, all downstream `upstream_failed`, run `failed`, **make exit 2** |

¹ `make pipeline CONF='{"allow_download": false, "dataset_dir": "/opt/airflow"}'`
(`DatasetMissingError`). The time is mostly the two retry back-offs.

**Object listing** (`s3://releaselens-data/raw/travistorrent/version=5d359345fecf0bf47be6edb10b5aae58/`).
After runs 1, 2 and 3 it was **identical**: key, size, ETag, metadata sha256 and LastModified, so
nothing was rewritten. Total 207,805,514 bytes, 3,881,992 rows:

| Key | Rows | Bytes | sha256 (prefix) |
|---|---|---|---|
| `lang=go/part-00000.parquet` | 255,358 | 19,760,962 | `dc7fa347f50fc2d5` |
| `lang=java/part-00000.parquet` | 401,767 | 37,021,131 | `43ab839685b0993c` |
| `lang=python/part-00000.parquet` | 1,520,967 | 82,205,359 | `ee5dbff52442def7` |
| `lang=ruby/part-00000.parquet` | 1,703,900 | 68,800,494 | `750d0a23b5844e7a` |
| `_manifest.json` | — | 17,568 | `0ae402352441b812` |

**Memory**

| Container | Cold run (1) | Repeat run (2) |
|---|---|---|
| `airflow-cli` (Airflow + DuckDB subprocess) | docker stats max 1,706 MiB · anon max 1,687 MiB · `oom_kill` 0 | 213 MiB · anon 258 MiB |
| `minio` | docker stats max 135 MiB · anon max 98 MiB · `oom_kill` 0 | 135 MiB |

`memory.peak` touched the `airflow-cli` and `minio` limits during the cold run. That is page cache
from streaming the 264 MB input and writing Parquet, which the kernel reclaims. Anonymous memory
stayed below both limits, and no OOM kill happened. The re-conversion run peaked at 1,458 MiB
(docker stats) / 1,519 MiB (anon).

**Full deployment smoke test** (`make up-airflow`: healthy in 35 s, `/api/v2/monitor/health` all
healthy). A triggered run (`airflow dags trigger`, LocalExecutor, idempotent path) succeeded in
18.4 s, and the task logs were served by the API from the shared logs volume. docker stats after the
run: api-server 245 MiB, scheduler 387 MiB, dag-processor 147 MiB, triggerer 185 MiB (≈ 0.96 GiB in
total). Max anon: 238 / 316 / 162 / 172 MiB.

**Review re-run (2026-10-13, after the review fixes; timings from the Airflow DB and `date`).**

| Run | `make pipeline` wall | DAG run | Result |
|---|---|---|---|
| repeat | 39 s | 7.3 s | `conversion skipped`, `uploaded=0, skipped=5`; listing JSON byte-identical to run 3 above (incl. ETag, sha256, LastModified) |
| read-only fallback² | 37 s | 5.4 s | warning logged, verified copy located under `/lake/downloads/…`, conversion and uploads skipped |
| failure test¹ | 300 s | 168.9 s | `download_or_locate_dataset` failed on try 3, the other 12 tasks `upstream_failed`, **make exit 2** |
| full deployment, triggered | — | 28.7 s | the four implemented tasks succeeded on try 1; 28080 and 29000 (bound to `127.0.0.1`) answer on the host's `localhost` |

² `CONF='{"dataset_dir": "/datasets/travistorrent/absent"}'`, with the dataset first copied into
`/lake/downloads/travistorrent-figshare-19314170/` (as uid 10001, which proves the dir is writable);
no network download was made, and the copy was deleted afterwards.

**Images** (`docker images`, containerd store, unpacked; compressed size from `docker image inspect`):

| Image | Size | Compressed | Note |
|---|---|---|---|
| `rlens/python:dev` | 1.05 GB | 238 MB | Now includes the polars runtime (164 MiB), scipy (148 MiB) and duckdb (52 MiB), plus the httpfs extension (19.9 MB) |
| `rlens/airflow:dev` | 1.42 GB | 319 MB | = `rlens/python` + one 293 MB layer (`/opt/airflow-venv`, 280 MB; no pip, no bundled tests) |
| `rlens/airflow:dag-test` | 1.53 GB | — | Test-only target (pytest + mypy). Not used by compose |

## 7. Known noise and caveats

- Each `external_python` task logs a `ModuleNotFoundError: No module named 'pendulum'` traceback.
  It comes from Airflow probing the target venv for pendulum, which the workspace venv
  deliberately lacks. It is harmless.
- During retry back-off, `airflow dags test` logs "No tasks to run" once per second.
- The DuckDB httpfs extension is baked into `rlens/python` (`/home/app/.duckdb`, owned by uid
  10001), so containers never download extension code at run time.
- `/lake` is a Docker volume, separate from the host's `data/lake`. Conversions on the host and in
  containers produce the same bytes (verified above), so they never fight over the S3 objects.
- `make doctor` (prerequisite of every heavy target) refuses to run below `MIN_FREE_GB` (default 2)
  GiB of free host disk; the M2b runs used `make … MIN_FREE_GB=1` on a host with 1.9–2.0 GiB free.
- Deterministic failures (missing dataset with downloads off, schema mismatch, data-quality gate)
  are retried like transient ones: the external-python boundary only reports an exit code, so the
  DAG cannot tell them apart. Back-off is 30 s then 60 s; the failure test above took 168.9 s.
- Do not run `make pipeline` while the `airflow-full` scheduler is running a DAG run: both write
  the same `/lake` version directory, and `max_active_runs=1` only applies within one scheduler.
- `airflow-triggerer` sometimes needs longer than compose's 10 s stop grace and exits 137 on
  `make stop-data`. Not reproduced on a freshly started triggerer, which stopped in 2 s with exit 0.

## 8. dbt (M3a): staging → intermediate → marts → ML inputs and features

### 8.1 Layout and how it runs

The dbt-duckdb project lives in `data/dbt` (dbt-core **1.12.5**, dbt-duckdb **1.11.0**, duckdb stays
**1.5.6**: exact pins in `rl-pipeline`, ADR-0014). `rl_pipeline.dbt_runner` runs it with dbt's
programmatic `dbtRunner`, one `dbt build` per stage (models **and** their tests; any failing
model or test fails the stage, and the task raises `DbtStageError` with the failing nodes):

| Stage (DAG task) | Selection | Materialised as | Data tests |
|---|---|---|---:|
| `dbt_deps` | — | (no packages yet; `dbt deps` logs "No packages were found") | — |
| `dbt_build_staging` | `source:travistorrent staging` | tables (+ one view) in `<RL_LAKE_DIR>/dbt/releaselens.duckdb` | 30 |
| `dbt_build_intermediate` | `intermediate` | tables in the DuckDB file | 14 |
| `dbt_build_marts` | `marts` | Parquet: `s3://releaselens-data/marts/<model>/run_id=<run_id>/part-00000.parquet` | 27 |
| `build_feature_snapshot` | `ml` | Parquet: `marts/<model>/run_id=<run_id>/` (inputs, cut points) and `features/release_features/schema=<FEATURE_SCHEMA_VERSION>/run_id=<run_id>/part-00000.parquet` + a deterministic `_manifest.json` | 23 |

- `--indirect-selection buildable`: a test that spans layers (e.g. `fct_builds` against
  `stg_travis_builds`) runs in the stage that builds its last parent. All 94 data tests run once.
- A stage whose selection builds no model fails (`DbtStageError`): otherwise it would "succeed"
  and the measurement (and, for `ml`, the manifest) would describe an older file of the same run id.
- **Profiles** (`data/dbt/profiles.yml`, in the repo, no secrets): target `dev` reads the raw
  Parquet with httpfs and an S3 secret built from `RL_S3_ACCESS_KEY` / `RL_S3_SECRET_KEY` /
  `RL_S3_REGION` / `RL_S3_ENDPOINT` (scheme stripped to host:port, `url_style path`, `use_ssl`
  from the scheme: false for the local `http://` endpoint). `threads: 2`, DuckDB `memory_limit`
  = `RL_DUCKDB_MEMORY_LIMIT` (1GB), `TimeZone UTC`, spill under `<RL_LAKE_DIR>/dbt/.duckdb_tmp`
  capped at `min(RL_DUCKDB_MAX_TEMP_MIB, free disk − 256 MiB estimate − 256 MiB reserve)` (the
  conversion's guard, `InsufficientDiskError` when nothing fits). Target `fixture` uses the same
  settings on local Parquet only (no extension, no S3).
- **Vars** (set by the runner): `dataset_version` (md5 of the primary file), `run_id` (the Airflow
  run id or `DBT_RUN_ID`; an id that is already path-safe is kept as is, any other is cleaned to
  `[A-Za-z0-9._-]` **plus `-` and 10 hex digits of the original id's sha256**, so two different
  run ids such as `a:b` and `a/b`, or two long ids sharing a 128-character prefix, never share an
  output prefix (unless an id is deliberately set to another id's sanitised form, hash
  included); Airflow's `manual__2026-10-13T03:00:00+00:00` becomes
  `manual__2026-10-13T03_00_00_00_00-<10 hex>`), `raw_root`, `output_root`,
  `feature_schema_version` (from the installed `rl_core`; `mart_build_features` raises if they
  differ). Overridable through task params `vars`: label-rate bounds and split fractions.
- Work dir `<RL_LAKE_DIR>/dbt` (DuckDB file, `target/`, `logs/`, `dbt_packages/`): the project
  directory stays read-only (in the Airflow image it is `/opt/airflow/dbt`). Usage stats are off.
- Output files: one per model and run, `run_id=` and `schema=` are path segments only (reads use
  `hive_partitioning = false`). Every S3 key the dbt stages write contains `run_id=<run_id>`
  (verified on the bucket listing, §8.7), so a run never writes into another run's prefix. A re-run
  with the **same** `run_id` (an Airflow retry, or `make dbt-build` with an id used before)
  overwrites that run's files; with unchanged code and data the bytes are identical (§8.7).
- Only the primary dataset has dbt models. `make pipeline DATASET=travistorrent-final-2017-01-25`
  skips the five dbt tasks (exit code 99 → `skip_on_exit_code`).

### 8.2 Models (rows from the real build; identical in runs `m3a-run2`, `m3-run1`, `m3-review1/2`)

| Layer | Model | Grain | Rows |
|---|---|---|---:|
| staging | `stg_travis_job_rows` (view) | every raw Java job row + `is_listed_in_build` and preference ranks | 256,055 |
| staging | `stg_travis_jobs` | one row per `tr_job_id`, job-level columns | 234,391 |
| staging | `stg_travis_builds` | one row per `tr_build_id`, typed and normalised | 128,819 |
| intermediate | `int_build_history` | **naive** predecessor per project by `started_at` (analytics only) | 128,819 |
| intermediate | `int_change_profile` | total churn (src + test) bucket, test-change-ratio bucket | 128,819 |
| intermediate | `int_build_timing` | UTC date/month/hour/weekday, duration | 128,819 |
| marts | `fct_builds` | one row per build | 128,819 |
| marts | `dim_project` | project: language, first/last build, volume, failure rate (+95% Wilson CI) | 402 |
| marts | `dim_time` | gap-free UTC date spine 2011-06-20 → 2015-08-19 | 1,522 |
| marts | `mart_build_reliability` | project × month: builds, failures, rate (+CI) | 6,010 |
| marts | `mart_change_risk` | {churn bucket, test-ratio bucket} × bucket: builds, failures, rate (+CI) | 12 |
| marts | `mart_ci_performance` | duration percentiles overall / by status / by project | 407 |
| ml | `mart_build_inputs` | one row per build: timeline, label, every rl_core build input | 128,819 |
| ml | `split_cutpoints` | one row: chronological cut timestamps and split sizes | 1 |
| ml | `mart_build_features` | one row per **labelled** build: 39 rl_core features + split + audit | 128,617 |

### 8.3 Staging decisions (measured on the real Java partition)

- **Deduplication.** No two raw rows are identical apart from `row` (0 duplicates) and no
  `(tr_build_id, tr_job_id)` pair repeats (256,055 distinct pairs), so the 21,664 extra rows are
  not copies: they are jobs attached to a second build (DATA_DICTIONARY → Evidence).
  `stg_travis_jobs` keeps, per job id, the row listed in its own build's `tr_jobs`, else the
  lowest dump row: 234,184 of 234,391 kept rows are listed; 16,563 job ids had more than one row
  and 43 of them have no listed row. `stg_travis_builds` is built from **all** raw rows of a build
  (canonical row: listed first, then lowest `tr_job_id`, `row`), so the 78 builds that have no
  listed row of their own are kept: 128,819 builds, exactly the source's distinct `tr_build_id`s
  (`no_duplicate_builds` test against the source).
  Re-verified in review (2026-10-13) against an independent recomputation on the raw Parquet: the
  kept row equals the expected one (listed first, else lowest `row`) for all 234,391 job ids
  (0 differences in row or build), no job id is listed in more than one build, and every kept
  job's build exists in `stg_travis_builds`.
- **`build_number` of the 78 builds without a listed row is NULL.** A foreign row carries the
  build number of the job's real build: on all 21,573 foreign rows whose job is listed in another
  build, `tr_build_number` equals that owner build's number, and it differs from the host build's
  own listed rows on 21,142 of 21,680 foreign rows in builds that have listed rows. `tr_num_jobs`
  and `git_num_committers` of foreign rows agree with the host build's listed rows (0
  differences), so they stay. Tested by `(build_number is null) = (n_listed_job_rows = 0)`.
- **Build-level consistency (test (d)).** `consistent_within_build` on `stg_travis_job_rows`
  proves on every run that the 30 raw build-level columns `stg_travis_builds` takes from the
  canonical row are identical over **all** job rows of every build (NULL mixed with a value counts
  as a disagreement). `tr_build_number`, `tr_num_jobs` and `git_num_committers` are checked over
  the build's **listed** rows only: a foreign row carries its own build's number, and the two
  shifted rows (cast-failure NULLs) are ignored there. Both pass.
- **Status / label.** `status = lower(trim(tr_status))`; `label` 1 = failed (22,068) or errored
  (15,267), 0 = passed (91,282), NULL = canceled (199) or started (3). 128,617 labelled builds,
  37,335 positive (29.03%).
- **Time.** `started_at = timezone('UTC', tr_started_at)` (timestamptz; `tr_started_at` is UTC,
  measured in M2b). `finished_at_upper = started_at + duration_s` (an upper bound: `tr_duration`
  sums job time), NULL for the 12 builds without a duration (9 errored, 3 started).
- **`tr_ci_latency`.** −1 is a placeholder: 6,756 builds at −1 against 12 at −2 and 22 at 0.
  Values below −1 (13,011 builds, minimum −58,865,485 s) are impossible for a latency. Both become
  NULL in `ci_latency_s`; `ci_latency_quality` = `placeholder` / `negative` / `ok` (109,052). It
  is post-build: never a model input.
- **`tr_prev_build` is not a previous-build link.** It is job-level (kept, −1 → NULL, as
  `stg_travis_jobs.travis_prev_build_id`; 206,187 kept jobs have a value) and dropped from the
  build grain. On the canonical rows: −1 on 15,905 builds, of which only 58 are the first build of
  their project; of the 112,914 builds with a positive link, the linked id is a Java build of this
  release for 11,513, a build of the **same project** for 49, and equal to the project's previous
  build by `started_at` for **0** of 112,570 (also 0 when any job row's link is allowed).
  History is therefore ordered by `started_at` (naive analytics) or by rl_core's finished-before
  rule (features), never by this column.
- **Negative test counts** (17 job rows, minimum −15) → NULL in `stg_travis_jobs`.
- **Dropped:** `gh_first_commit_created_at` (a copy of `tr_started_at` in UK local time), free text,
  `git_merged_with` and the comment counts (uncertain). `gh_description_complexity` is kept in
  staging as `description_complexity_uncertain` for analysis only.

### 8.4 ML inputs: rl_core build inputs ← 2016 columns (`ml/mart_build_inputs`)

`mart_build_inputs` holds every Java build (128,819; canceled/started included: rl_core never
counts a build without an outcome as history, but its start still counts for
`hours_since_last_build`) with `build_id, project, started_at, finished_at (= finished_at_upper),
failed (= label), duration_s, label` and the inputs:

| rl_core input | 2016 column | Decision and evidence |
|---|---|---|
| `src_churn`, `test_churn` | `gh_src_churn`, `gh_test_churn` | direct |
| `files_added` / `_deleted` / `_modified` | `gh_files_*` | direct |
| `tests_added` / `_deleted` | `gh_tests_*` | direct; behave like test-**case** counts (DATA_DICTIONARY → Evidence) |
| `src_files`, `doc_files`, `other_files` | `gh_*_files` | direct; per change, not per repository (Evidence) |
| `sloc`, `test_lines_per_kloc`, `test_cases_per_kloc` | `gh_sloc`, `gh_test_lines_per_kloc`, `gh_test_cases_per_kloc` | direct |
| `asserts_per_kloc` | `gh_asserts_cases_per_kloc` | direct (renamed) |
| `team_size`, `by_core_team_member`, `is_pr` | `gh_team_size`, `gh_by_core_team_member`, `gh_is_pr` | direct |
| `commits_on_files_touched` | `gh_commits_on_files_touched` | direct |
| `num_all_built_commits` | `git_num_commits` | **mapped.** `git_num_commits` = entries of `git_commits` (100% of rows). `git_commits` never contains the built commit, but contains the project's previous build's (by `started_at`) commit on 78,156 of 128,417 builds (60.9%); single-entry lists equal that commit on 64,196 of 104,726 (61.3%); a build's own commit appears in another build's `git_commits` for 94,560 of 128,819 builds (73.4%). So the list is the commits between the previous built commit and this one, offset by one, and its length is the number of commits this build newly builds — the concept of `num_all_built_commits`. Taking the previous build on the **same branch** raises the share to 72.0% (85,603 of 118,880); the rest is not explained here. Minimum 1. |
| `num_commits_in_push` | — → NULL | not in this release (GHTorrent push events); ADR-0004 excludes it rather than approximating it differently offline and online. The push and the built-commit set coincide only when every push is built, which this data cannot show |
| `description_complexity` | — → NULL | `gh_description_complexity` is tagged **uncertain** (final PR text, editable after the build): excluded from model inputs by default |
| `repo_age_days` | — → NULL | `gh_first_commit_created_at` equals `tr_started_at` in UK local time on 256,015 of 256,055 job rows: not the repository's first commit. No other column carries repository age |

The three NULL inputs are asserted by the test `mart_build_inputs_unavailable_inputs_are_null`;
rl_core turns them into NaN features (`num_commits_in_push`, `description_complexity`,
`project_age_days`), which M4 must drop or accept as all-missing.

### 8.5 Feature snapshot and the chronological split

`ml/mart_build_features` is a dbt-duckdb **Python** model (ADR-0006). It calls
`rl_pipeline.feature_snapshot.build_feature_snapshot`, which runs
`rl_core.features.offline.compute_features` on all of `mart_build_inputs`, keeps the labelled
builds and adds `split` from `ml/split_cutpoints` (DuckDB ↔ Polars via the Arrow PyCapsule
stream, so no pyarrow dependency). Columns: `build_id, project, started_at, label, split`, the 39
`ALL_FEATURE_NAMES`, and rl_core's audit column `max_history_input_finished_at` (epoch seconds).

Split: the labelled builds ordered by `(started_at, build_id)`; the cut points are the
`started_at` of the first build after the oldest 70% and after the oldest 85%, and builds are
assigned by timestamp (ties never straddle a cut). Measured (identical in both runs):

| Split | From (UTC) | To (UTC) | Builds | Positives | Label rate |
|---|---|---|---:|---:|---:|
| train | 2011-06-20 13:04:06 | 2015-01-10 07:40:01 | 90,031 | 26,152 | 29.05% |
| validation | **2015-01-10 08:05:34** (cut) | 2015-04-17 18:31:15 | 19,293 | 5,800 | 30.06% |
| test | **2015-04-17 18:41:22** (cut) | 2015-08-19 02:52:38 | 19,293 | 5,383 | 27.90% |
| all | | | 128,617 | 37,335 | 29.03% |

- The overall label rate 29.03% is inside the `label_rate_between` bounds [0.05, 0.60] (tested on
  `fct_builds` and `mart_build_features`).
- **Cut points (review, 2026-10-13).** The last train start is 2015-01-10 07:40:01 and the last
  validation start 2015-04-17 18:31:15, both strictly before the next cut; exactly one labelled
  build starts at each cut timestamp, so no tie straddles a cut (655 start timestamps are shared
  by 2+ labelled builds elsewhere). `split_cutpoints_are_chronological` now requires strictly
  ordered cut points and three non-empty splits (the former `<=` version accepted equal cut points,
  i.e. an empty validation split).
- **Label availability at the cuts** (new `split_cutpoints` columns, also in the manifest): a
  build's outcome is observable at its upper-bound finish. 9 train builds have no finish time (no
  duration) and 0 train builds finish at or after `validation_start_at` (latest train finish
  2015-01-10 08:03:40); 8 validation builds finish at or after `test_start_at` (latest validation
  finish 2015-04-17 20:47:39). Start times are strictly ordered; M4 may purge these 9 + 8 builds
  if it needs an embargo.
- Leakage audit: 128,169 rows have history inputs; `max_history_input_finished_at < started_at`
  holds for all of them (dbt test `finished_before_started`, re-checked by the task: 0
  violations). That audit column is computed by rl_core itself, separately from the features, so
  it cannot reveal a history window that admitted a build finishing at or after the prediction
  moment. The new test `history_window_matches_inputs` recomputes the window from
  `mart_build_inputs` in plain SQL (builds of the project with an outcome that finished strictly
  before `started_at`): `history_size` and the audit column equal it on all 128,617 rows. In the
  review the same oracle was extended by hand (ASOF join and per-project ranks, ties broken like
  rl_core) to `prev_build_failed` and `prev_build_duration` (0 differences on the 128,169 rows
  with history) and to `fail_rate_last_5`, `fail_rate_last_20` (|difference| <= 1e-12) and
  `builds_since_last_failure` (exact): 0 differences on all 128,617 rows.
  The SQL is a test oracle only; every feature value still comes from rl_core (ADR-0006).
- Missing values (NULL) among the 128,617 rows: `num_commits_in_push`, `description_complexity`
  and `project_age_days` on all rows (§8.4); the history features on the 448 rows without a
  finished earlier build (`prev_build_failed`, `fail_rate_last_5/20`, `prev_build_duration`; the
  audit column too), `hours_since_last_build` on 404, `builds_since_last_failure` on 4,104 (no
  failure yet) and the duration z-score on 1,261; every other feature is complete.
  Types: `build_id` BIGINT, `started_at` TIMESTAMPTZ, `label` INTEGER, `split` VARCHAR, features
  and audit DOUBLE.
- `features/.../run_id=<id>/_manifest.json`: dataset version, feature schema version and names,
  file rows/bytes/sha256, cut points, rows and label balance per split, input files' sha256.

### 8.6 Tests

94 data tests: `unique`/`not_null` on every key, `accepted_values` (status, label, language,
buckets, split), `relationships` (`fct_builds.project → dim_project`,
`fct_builds.started_date → dim_time`, `stg_travis_jobs.build_id → stg_travis_builds`), and the
custom generic tests in `data/dbt/tests/generic/`:

| Test | Asserts |
|---|---|
| `finished_before_started` (a) | rl_core audit column < `started_at` on every `mart_build_features` row |
| `history_window_matches_inputs` (a') | independent oracle: `history_size` and the audit column equal a plain-SQL recount over `mart_build_inputs` of the project's builds with an outcome that finished strictly before `started_at` (§8.5) |
| `label_rate_between` (b) | share of label 1 among labelled rows within [`label_rate_min`, `label_rate_max`] |
| `no_duplicate_builds` (c) | one non-NULL row per build, and (optionally) exactly the build ids of a reference relation (staging vs source, intermediates/marts/ml vs staging, features vs labelled inputs) |
| `consistent_within_build` (d) | build-level columns agree over a build's job rows (§8.3) |
| `expression_is_true` | row invariants: finish = start + duration (staging and `mart_build_inputs`), no negative placeholder among the rl_core inputs, label follows status, `build_number` NULL exactly for builds without a listed row, strictly ordered cut points with three non-empty splits, split follows the cut points, CI contains the rate, percentiles ordered, … |

**Every custom test can fail (review, 2026-10-13).** The compiled SQL of all 31 custom test
nodes of run `m3-review2` was executed against an in-memory copy of every relation they read
(the dbt DuckDB file attached read-only, the raw Java Parquet materialised): each returns 0 rows
on the real data, and each of 39 targeted mutations (one or two per node, each rolled back) makes
its test return rows, 39 of 39. Two mutations show why tests were strengthened: raising one row's
`history_size` by 1 (a window that admitted one more build) leaves `finished_before_started` at 0
rows while `history_window_matches_inputs` returns it; equal validation and test cut points
(an empty validation split) pass the former `<=` chronology expression and fail the new one.
`test_dbt_fixture.py::test_leakage_oracle_runs_and_catches_a_window_the_audit_misses` repeats the
first one on the fixture in CI. Tests that hold by construction of their model (e.g.
`failed is not distinct from label`, the three NULL inputs, finish = start + duration) are kept as
regression guards; the mutations prove their SQL detects a violation.

`dbt build` fails on a failing test: the first real run failed on
`mart_build_reliability_interval_contains_rate` (51 rows): without rl_core's clamping, the
Wilson bounds can miss an estimate of exactly 0 or 1 by floating-point rounding (recomputing the
unclamped formula, every violating row has a rate of 0 or 1). The macro now uses rl_core's clamping and the test passes;
`test_dbt_fixture.py::test_failing_dbt_test_fails_the_task` forces a `label_rate_between` failure
and expects the task to raise.

The fixture target (`make dbt-fixture`, CI `data.yml`, `test_dbt_fixture.py`) converts the
SYNTHETIC 2016 fixture with the real converter and runs the whole project on it (8 builds, 6
labelled, split 4/1/1): every test passes, the Python mirror of the output layout matches the
macro, and a rebuild under another run id is byte-identical.

### 8.7 Measured runs (`make dbt-build`, host, 2026-10-13)

Both runs: `make dbt-build DBT_RUN_ID=m3a-run<N> MIN_FREE_GB=0` with `RL_DUCKDB_MAX_TEMP_MIB=128`
(the host had 0.94–0.99 GiB free, below `doctor`'s threshold; a run writes ~30 MB locally).
Timings from the task summary (`elapsed_s` per stage) and `/usr/bin/time -l`.

| Run | `make` wall | deps / staging / intermediate / marts / ml (s) | Peak RSS | dbt nodes |
|---|---:|---|---:|---|
| `m3a-run1` | 9.90 s | 0.13 / 2.49 / 0.39 / 1.16 / 2.32 | 1.02 GB | 106 PASS/OK (15 models, 91 tests), 0 errors |
| `m3a-run2` | 9.64 s | 0.13 / 2.39 / 0.40 / 1.09 / 2.18 | 0.94 GB | same |
| `m3a-run2` again (after a runner change) | 9.15 s | 0.14 / 2.30 / 0.49 / 0.78 / 2.11 | not recorded | same; all 9 files overwritten with identical bytes |

Outputs (bytes and sha256 **identical in both runs**; only the `run_id=` path differs):

| Object | Rows | Bytes | sha256 (prefix) |
|---|---:|---:|---|
| `marts/fct_builds/…` | 128,819 | 3,016,347 | `24c1856820051fc4` |
| `marts/dim_project/…` | 402 | 25,409 | `52d67e61db30f362` |
| `marts/dim_time/…` | 1,522 | 6,050 | `2a664cee82103a73` |
| `marts/mart_build_reliability/…` | 6,010 | 81,445 | `929d59961d388696` |
| `marts/mart_change_risk/…` | 12 | 2,361 | `5ce9ee01ddadc9b1` |
| `marts/mart_ci_performance/…` | 407 | 17,980 | `1675bae3f0488947` |
| `marts/mart_build_inputs/…` | 128,819 | 4,623,246 | `f952cd4424e4ea3d` |
| `marts/split_cutpoints/…` | 1 | 1,930 | `c378ec26b02226a9` |
| `features/release_features/schema=1.0.0/…` | 128,617 | 7,362,393 | `384f66b66bde7416` |

The two `_manifest.json` files (2,722 bytes each) differ only in `run_id` and the input locations.
The DuckDB file measured 80 MB after run 1 and 28,848,128 bytes after run 2; `target/` 2.8 MB,
`logs/` 0.8 MB. No credential appears in `target/`, `logs/`
or the console output (grep for the secret: 0 hits).

**Review rebuilds (2026-10-13, after the review fixes).** `m3-run1` (an earlier run of the same
models, already in the bucket) has the same sha256 as `m3a-run1/2` for all 9 files. Two new runs
on the real data, `RL_DUCKDB_MAX_TEMP_MIB=128`, timed with `/usr/bin/time -l` while other jobs
loaded the host (hence slower than above):

| Run | Command | Wall | deps / staging / intermediate / marts / ml (s) | Peak RSS | dbt nodes |
|---|---|---:|---|---:|---|
| `m3-review1` | `make MIN_FREE_GB=1 dbt-build DBT_RUN_ID=m3-review1` | 47.61 s | 0.80 / 11.45 / 3.11 / 3.37 / 9.91 | 1,006,731,264 B | 109 PASS/OK (15 models, 94 tests), 0 errors |
| `m3-review2` | `uv run --no-sync python -m rl_pipeline dbt_build_all --params-json '{"run_id": "m3-review2"}'`¹ | 57.63 s | 0.77 / 14.29 / 3.08 / 4.15 / 20.59 | 936,984,576 B | same |

¹ The command `make dbt-build` runs after `doctor`; `doctor` refused with `MIN_FREE_GB=1` because
other jobs had left the host just under 1 GiB free, so the same command was run directly (the
runner's own disk guard still applied).

All 9 files of `m3-review1` and `m3-review2` have identical sha256 and size. Against `m3-run1`,
8 of 9 are identical (`fct_builds`, the five other marts, `mart_build_inputs` and the features
file, sha256 `384f66b66bde7416957c189aeccd9f55e369bbb7d9acf726492371a1a25569a5`, 7,362,393 bytes:
the review changed no feature value); `split_cutpoints` differs because of its two new columns
(2,348 bytes, sha256 prefix `107cdb89fb2391a9`). The two review manifests (2,837 bytes) differ
only in `run_id` and the input locations. The bucket holds 51 dbt objects, each under a
`run_id=` segment. The DuckDB file is 28,848,128 bytes after the review runs; `target/` 2,976 KiB,
`logs/` 2,920 KiB; the secret appears in none of them nor in the console output (0 files).

### 8.8 What the marts show (descriptive; associations, not causes)

Failure rate (label 1 / labelled builds) with 95% Wilson intervals, `mart_change_risk`:

| Total churn (lines) | Builds | Failures | Rate | 95% CI |
|---|---:|---:|---:|---|
| 0–100 | 94,546 | 26,557 | 28.09% | 27.80–28.38% |
| 101–500 | 22,760 | 6,910 | 30.36% | 29.77–30.96% |
| 501–1,000 | 5,338 | 1,725 | 32.32% | 31.07–33.58% |
| 1,001–5,000 | 4,932 | 1,765 | 35.79% | 34.46–37.14% |
| >5,000 | 1,041 | 378 | 36.31% | 33.45–39.28% |

Larger changes coincide with more failures; project, period and author are not controlled for,
so this is not an effect of change size. By test share, builds that change only tests fail most
often (34.29%, 6,987 labelled builds) and builds without line churn least often (27.72%,
34,413); the middle buckets are not monotone (26–50%: 29.42%, 1–25%: 32.68%).

Duration (`mart_ci_performance`, summed job time): p50 452 s, p90 2,505 s, p99 7,521 s over
128,807 builds; p50 by status: passed 420 s, failed 549 s, errored 623 s, canceled 318 s.

Naive history (`int_build_history`): for 28,211 of the 128,417 builds that have a predecessor
(21.97%), that predecessor's upper-bound finish is not before the build's start — a naive
"previous build failed" feature would have used an outcome that was not yet knowable for about
one build in five (an upper bound overstates it). rl_core's finished-before rule avoids this.

### 8.9 Not verified, and caveats

- **The dbt tasks have not run inside the Airflow container yet.** `uv.lock` changed (dbt and its
  dependencies), so `rlens/python` and `rlens/airflow` must be rebuilt (`make build-data`) before
  `make pipeline` can run them; that build needs ~2 GB of Colima disk (2.1 GB free) and the host
  was below 1 GiB free, so it was not attempted. What was verified: the 37 DAG-integrity tests
  and mypy pass in the existing `rlens/airflow:dag-test` image with the new DAG, tests and
  `rl_pipeline` mounted (Airflow 3.3.2 parses the DAG; the dbt task bodies exit with the skip code
  for the secondary dataset), and the same task functions ran the real build on the host (§8.7).
- `protobuf` moved from 7.36.2 to 6.33.6 and `pathspec` from 1.1.1 to 1.0.4 in `uv.lock` (dbt's
  upper bounds). The OTLP exporter imports, and the rl_core (325) and services/demo/integrations
  (125) unit tests pass with them.
- dbt re-parses fully on every invocation ("profile has changed"): the spill cap in the profile
  depends on free disk. A full parse takes about 1 s.
- `make dbt-build` runs on the host with `--no-sync` (the current workspace venv). The DuckDB
  httpfs extension is installed into `~/.duckdb` on first use (the images bake it in).
- **The finish-time upper bound is an assumption for multi-job builds.** `finished_at =
  started_at + tr_duration` bounds the wall-clock finish when a build's jobs run in parallel or
  back to back. If jobs waited in Travis' queue between each other (concurrency limits), the real
  finish could be later than that bound, and the finished-before rule would admit a build whose
  outcome was not yet known. The release has no finish time, so this cannot be measured here.
  Exposure: 51,685 of 128,819 builds have more than one job, and for 6,233 of the 128,169 feature
  rows with history the most recent eligible build is such a build whose bound lies less than
  10 minutes before the row's start (17,726 rows when that build may have any job count).
- **SQL safety (bandit B608).** Since the review, every value in `rl_pipeline`'s DuckDB SQL (paths,
  null tokens, cast patterns, the timestamp format, partition values, COPY options, the S3 secret)
  is a bound parameter (`SqlParams`, or a DuckDB relation where a view cannot take parameters).
  Identifiers come from the fixed schemas and pass `schema.sql_ident` (plain-identifier regex,
  allowlisted against the spec's columns where they are spliced); the statements that splice them
  carry `# nosec B608` on the flagged line. `bandit -ll` reports 0 issues for `data/pipeline`.
  Output unchanged (2026-10-13): the 2016 release re-converted with the refactored code into a
  scratch lake (`RL_DUCKDB_MEMORY_LIMIT=4GB`, 47.85 s) gives exactly the published sha256 for both
  Parquet files and `_manifest.json` (`ff8cc162…dc20`, `a8cb2629…3d3a`, `59544c06…b3e`, §6.1), and
  its data dictionary rebuilt from that output (with the UTC cross-check) is byte-identical to
  `DATA_DICTIONARY.md` and `data_dictionary.json`. The 2017 source file was no longer in
  `data/lake/downloads/travistorrent-figshare-19314170/` (the directory was emptied at
  10:20:30 local time, outside this review), so the 2017 release was not re-converted; instead its
  dictionary was rebuilt with the refactored code from the published Parquet (copied from the
  bucket into a scratch dir) and is byte-identical to `DATA_DICTIONARY_final_2017.md` and
  `data_dictionary_final_2017.json`, and both SYNTHETIC fixtures (2016 and 2017 layouts) convert
  to byte-identical Parquet, manifests and dictionaries with the code before and after the
  refactor. All scratch output was deleted.
