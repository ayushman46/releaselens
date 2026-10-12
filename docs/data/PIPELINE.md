# Historical pipeline (M2): TravisTorrent → Parquet → S3

> Orchestration for `historical_pipeline` (spec §7.2, ADR-0004, ADR-0009, ADR-0013). The task code is
> `rl_pipeline` (`data/pipeline`, Airflow-agnostic). Airflow only wires it together. Every number in
> §6 was measured on the dev machine on 2026-10-13 (Colima: 2 CPU / 4 GiB) with the commands shown.

## 1. How to run

| Command | What it does |
|---|---|
| `make up-data` | Builds `rlens/python` → `rlens/airflow` (in that order), starts `postgres` + `minio` (pgsty/silo) and waits until healthy, then runs `minio-init` (creates `releaselens-data` and `releaselens-mlflow`, idempotent). |
| `make pipeline` | `up-data`, then **`airflow dags test historical_pipeline`** in a one-shot `airflow-cli` container (`compose run --rm`). Its dependencies `minio-init` and `airflow-init` (`airflow db migrate`) run first. The exit code is non-zero when the DAG run fails. |
| `make pipeline CONF='{"skip_training": true}'` | Same, passing DAG params (`--conf`). |
| `make pipeline-test-dag` | DAG checks without a DB or S3. Builds the `dag-test` target (Airflow image + pytest + mypy, never shipped) and runs `mypy` (with Airflow's own types) and 20 pytest DAG-integrity tests. CI runs the same target build in `backend.yml` (`images` job). In the workspace venv (`make test`, CI unit jobs) the DAG test module is reported as skipped, because Airflow is not installed there. |
| `make up-airflow` | The full Airflow 3 deployment: `airflow-api-server` (UI/API on <http://localhost:28080>), `airflow-scheduler` (LocalExecutor), `airflow-dag-processor` and `airflow-triggerer`. Profile `airflow-full`. See §5 before using it on a 4 GiB VM. |
| `make stop-data` | Stops `minio` and the four Airflow services. Core services keep running. |
| `make ps` / `logs` / `down` | Now include the `data` and `airflow-full` profiles. |
| `make test` / `typecheck` / `test-integration` | Also cover `rl_pipeline` (`data/pipeline`). |

`make doctor` (a prerequisite of `up-data`) refuses to run when the host has less than
`MIN_FREE_GB` (default 2) GiB free. The data itself lives in Docker volumes inside the Colima VM.

### DAG params

| Param | Default | Meaning |
|---|---|---|
| `skip_training` | `true` | Short-circuits `train_models` / `evaluate_and_register` (default `true` until M4). |
| `dataset_dir` | `null` | Overrides `RL_DATASET_DIR` (the directory that holds `final-2017-01-25.csv.gz`). |
| `allow_download` | `true` | When the file is missing or corrupt: download it from Figshare (`true`) or fail (`false`). The compose mount of the dataset dir is read-only, so in that case the download goes to `/lake/downloads/travistorrent-figshare-19314170/` (see §2). |

## 2. Tasks

```
download_or_locate_dataset → validate_raw_schema → convert_to_parquet → load_raw_s3
  → dbt_deps → dbt_build_staging → dbt_build_intermediate → dbt_build_marts
  → export_analytics_to_postgres → build_feature_snapshot            (M3 placeholders)
  → training_enabled [short-circuit on params.skip_training]
  → train_models → evaluate_and_register                             (M4 placeholders)
```
`convert_to_parquet` consumes the located-dataset dict but also depends on `validate_raw_schema`, so it
never starts before the schema check has passed.

| Task | Runs in | Timeout | What it does |
|---|---|---|---|
| `download_or_locate_dataset` | workspace venv | 30 min | `rl_pipeline.tasks.locate_or_download`: finds the file and verifies the size plus the publisher's md5 (`5d359345…`). It downloads (resumable, md5-verified, `testroots.org` refused) only when the file is missing or corrupt. Returns `{path, md5, size, version=md5}`. If the dataset dir is not writable (the compose `:ro` mount) and holds no verified copy, it logs a warning and downloads to `<RL_LAKE_DIR>/downloads/travistorrent-figshare-19314170/` instead; the read-only copy is never touched. |
| `validate_raw_schema` | workspace venv | 10 min | Header must equal the 66 expected column names, in order. Raises on mismatch. |
| `convert_to_parquet` | workspace venv | 45 min | DuckDB streams the `.csv.gz` (never decompressed to disk). A data-quality gate runs first: per-column cast-failure rate ≤ 0.1 %, unique non-null `tr_job_id`, path-safe `gh_lang`. Then one typed, sorted, ZSTD Parquet file per language goes to `/lake/raw/travistorrent/version=<md5>/`, with `_manifest.json` written last. |
| `load_raw_s3` | workspace venv | 30 min | Uploads to `s3://releaselens-data/raw/travistorrent/version=<md5>/lang=<lang>/part-00000.parquet`, plus `_manifest.json` last. Verifies the listing sizes afterwards. |
| 6 × M3, 2 × M4 placeholders | Airflow venv | 1 min | Raise `AirflowSkipException("<task>: implemented in M3/M4 …")`. They are **skipped, never a fake success**. They use `trigger_rule=none_failed`, so each one runs and logs its own reason; an upstream failure still marks them `upstream_failed`. |
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

1. **Version = md5 of the source file**, re-verified on every run. Every path and S3 key carries `version=<md5>`.
2. **Conversion skip:** `convert_to_parquet` returns immediately (`"conversion skipped"`) when
   `/lake/.../version=<md5>/_manifest.json` exists for the same converter settings and schema
   fingerprint **and** every file's sha256 still matches.
3. **Upload skip:** every object carries `x-amz-meta-sha256`. An upload is skipped when the key
   already holds the same sha256. `_manifest.json` is written last, so its presence means the
   version is complete.
4. **Deterministic output:** rows are ordered by `(tr_build_id, tr_job_id)`, with a fixed row-group
   size and ZSTD. Measured: a from-scratch re-conversion in the container (fresh `/lake` volume)
   produced **byte-identical** Parquet (same sha256 for all 4 files, and also identical to a
   conversion run on the macOS host). S3 then skipped all 5 uploads.

## 4. Compose services (`infra/compose/compose.yaml`)

| Service | Profile | Image | Notes |
|---|---|---|---|
| `minio` | data | `pgsty/silo:RELEASE.2026-09-16T00-00-00Z` | Ports 29000 (S3) / 29001 (console), bound to `127.0.0.1` only (the root credentials are the public dev defaults). Volume `minio-data`. Healthcheck: `curl -f /minio/health/live` (the image ships curl and mc, but not wget or nc). `mem_limit 256m`. |
| `minio-init` | data | `rlens/python:dev` | `python -m rl_pipeline.s3 ensure-buckets` (one-shot, idempotent). |
| `airflow-init` | data | `rlens/airflow:dev` | `ensure_metadata_db.py` creates the `airflow` DB only if it is missing (the postgres init script only runs on a fresh volume), then `airflow db migrate`. Creates nothing else. |
| `airflow-cli` | data | `rlens/airflow:dev` | One-shot CLI used by `make pipeline`. `mem_limit 2g`, because the whole DAG, including DuckDB, runs inside it. |
| `airflow-api-server`, `-scheduler`, `-dag-processor`, `-triggerer` | **airflow-full only** | `rlens/airflow:dev` | LocalExecutor, so tasks run in the scheduler container (`mem_limit 2g`). They share a JWT secret and the `airflow-logs` volume. The UI runs without a login (`SIMPLE_AUTH_MANAGER_ALL_ADMINS`, dev only), so port 28080 is bound to `127.0.0.1` only. |

Mounts for every Airflow container:
- host dataset dir → `/datasets/travistorrent:ro` (`RL_DATASET_DIR`);
- volume `lake` → `/lake` (`RL_LAKE_DIR`, the Parquet staging area plus DuckDB spill);
- `data/airflow/dags` → `/opt/airflow/dags:ro` (DAG edits need no rebuild).

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
- Deterministic failures (missing dataset with downloads off, schema mismatch, data-quality gate)
  are retried like transient ones: the external-python boundary only reports an exit code, so the
  DAG cannot tell them apart. Back-off is 30 s then 60 s; the failure test above took 168.9 s.
- Do not run `make pipeline` while the `airflow-full` scheduler is running a DAG run: both write
  the same `/lake` version directory, and `max_active_runs=1` only applies within one scheduler.
- `airflow-triggerer` sometimes needs longer than compose's 10 s stop grace and exits 137 on
  `make stop-data`. Not reproduced on a freshly started triggerer, which stopped in 2 s with exit 0.
