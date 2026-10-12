# Migration audit (M0)

> Audit date: 2026-10-13 · Auditor: founding engineer (Claude Code) · Status: **for approval**

## 1. What exists today

### 1.1 Git history

| Item | Finding |
|---|---|
| Commits | Exactly one: `8283b39 Initial commit from Create Next App` (2026-10-06). |
| Remote | None configured. |
| Branch | `main`. |
| Committed content | Create Next App scaffold: `next@16.3.8`, `react@19.2.8`, Tailwind v4, ESLint 9, `babel-plugin-react-compiler@1.0.0`, default `src/app/{layout,page}.tsx`, `public/*.svg`. |
| Working tree | All 17 scaffold files are **deleted but not committed**. |

### 1.2 Untracked content

| Path | What it is | Verdict |
|---|---|---|
| `data/raw/travistorrent/java-builds/` | **2.1 GB**, 11 project folders, 82,668 `*.log.bz2` Travis job logs + per-project `repo-data-travis.{csv,json}`, `buildlog-data-travis.{csv,json}`, sometimes an `errors` file. This is a **partial** copy of the 20-project archive in `~/Downloads` (for example, DSpace has 1,193 files here and 3,820 in the full archive). | Do **not** commit. Replace it with a reproducible "locate dataset" step (M2). See §3. |
| `data/scripts/profile_travistorrent.py` | Empty file (0 bytes). | Replace with a real profiler in `data/analysis/` (M2). |
| `data/{notebooks,processed,reports}/`, `docs/`, `ml/{artifacts,evaluation,training}/` | Empty directories. | Superseded by the monorepo layout (`ml/`, `data/analysis/`, `docs/`). |
| `.venv/` | Python 3.12.11 venv with `duckdb 1.5.6`, `polars 2.0.0`, `pandas 3.0.6`, `pyarrow 25.0.1`, `numpy 2.5.3`. | Replace with a `uv` workspace (ADR-0002). Never commit it. |
| `.DS_Store` files | macOS metadata. | Ignore. |

### 1.3 The earlier prototypes

**No prototype source is in git or on disk.** The only commit is the Next.js scaffold. I searched `~`
(depth ≤ 4) for `*releaselens*` directories and `~` (depth ≤ 5) for `go.mod` files that mention
releaselens, and found nothing outside this repo. The Colima Docker engine still holds traces of
**two** earlier prototypes, both started from this directory (compose project `releaselens`,
config file `<repo>/docker-compose.yml`, now deleted):

| Trace | What it shows |
|---|---|
| Volume `releaselens_clickhouse-data` (plus `releaselens_postgres-data`, `releaselens_postgres-data-v2`) | The Go + Kafka + Postgres + ClickHouse prototype described in the brief once ran here. **Its source and images are gone**, so it cannot be audited. |
| Running containers `releaselens-backend` (image built 2026-10-07) and `releaselens-postgres` (`postgres:17`, up 6 days, host ports **8000** and **5432**) | A later, much smaller **Python** prototype. Its source survives inside the image. I copied it out read-only for this audit and changed nothing. |

**Audit of the surviving Python prototype (`ReleaseLens API` v0.2.0, `app/main.py`, 230 LOC, 2 tests):**

| Aspect | Finding | Verdict |
|---|---|---|
| Stack | FastAPI + sync SQLAlchemy + psycopg2, SQLite fallback, `Base.metadata.create_all` at startup | Replace: async SQLAlchemy + Alembic migrations (spec §4) |
| Tenancy | Tenant comes from a client-supplied `X-Tenant-ID` header (default `"demo"`), and **reads are unauthenticated**. Any caller can read any tenant's data by changing a header. | **Do not keep.** Derive the tenant from the API key or session only (ADR-0010) |
| Ingest auth | One static bearer token for all tenants; `tenant_id` is taken from the request body | Replace with per-tenant hashed, scoped API keys |
| Idempotency | None: no `event_id`, no unique constraint, `POST` returns 201 synchronously | Replace with the envelope + `(tenant_id, event_id)` design (M6) |
| Fabricated data | `/api/v1/overview` returns a **hard-coded** service list whose health flips to "warning" whenever any regression row exists | **Must not be carried forward** (violates "never fabricate numbers") |
| Regressions | Table holds p95/error before-after columns, but nothing computes them | Replace with the `rl_core` regression engine (M8) |
| Tests | `/health` + `/ready`, and "POST without token → 401" | Keep both scenarios as API tests in M1/M10 |
| Data | 0 rows in `deployments` and 0 in `regressions` | Nothing to migrate |

**Operational impact:** the old containers occupy host ports 8000 and 5432, and they belong to compose
project `releaselens`. To avoid touching them, the new stack uses compose project name
**`rlens`** and non-conflicting host ports (ADR-0015). Stopping or removing the old containers and
volumes is left to you (`docs/QUESTIONS_FOR_OWNER.md`).

## 2. Keep conceptually vs replace

The prompt describes the prototype. Based on that description, this is what carries forward. Each item will be re-implemented in Python.

| Concept from prototype | Decision | Where it lands |
|---|---|---|
| Versioned deployment-event envelope | **Keep.** Re-specify as `schema_version` + `event_id` (UUIDv7) + `tenant_id` + `occurred_at` + `type` + `payload`. | `rl_core/events`, M6 |
| `(tenant_id, event_id)` idempotency | **Keep.** `UNIQUE(tenant_id, event_id)` + `INSERT … ON CONFLICT DO NOTHING`. Commit the offset only after the DB commit. | `deployments` table, M6 |
| Idempotency/duplicate-delivery tests | **Keep the scenarios.** Rewrite with pytest + testcontainers (Kafka + Postgres). | `services/worker/tests`, M6 |
| Go services | **Replace** with Python 3.12 (FastAPI + confluent-kafka). Reason: one language for `rl_core`, so features, analysis logic, training and serving share code (no train/serve drift). | ADR-0002 |
| ClickHouse span store | **Replace** with partitioned Postgres. Revisit if the thresholds in the span-storage ADR are exceeded. | ADR-0008, M7 |
| Kafka | **Keep**, in KRaft mode. | ADR-0007 |
| Create Next App scaffold | **Recreate** under `apps/web` (keeping Next 16 / React 19 / Tailwind 4 from the scaffold, pinned in M1). Do not resurrect it at the repo root. | M1 / M11 |

## 3. Data on disk: findings that block or affect M2

1. **The dataset is not the official TravisTorrent CSV dump.** The spec assumes the 2017 build-level
   dump with `gh_*` change columns (`gh_src_churn`, `gh_test_churn`, `gh_team_size`, `gh_sloc`, …).
   What is on disk is an archive named `travistorrent-java-ci-build-dataset-master`. It holds raw
   Travis job **logs** plus two small tables per project:
   - `repo-data-travis.csv`: `build_id, commit, pull_req, branch, status, duration, started_at, jobs, event_type`
   - `buildlog-data-travis.csv`: `tr_build_id, tr_job_id, tr_build_number, tr_original_commit, tr_log_*`

   It has **no** change-size, test-change, codebase or team columns. The details and counts are in
   `docs/data/DATASET_AUDIT.md`. The data-source decision is ADR-0004.
2. **"java-builds" is a misnomer.** Several projects are Ruby or Python. The real per-project
   languages are in the dataset audit.
3. **The in-repo copy is partial** (11 of 20 projects, at least one truncated). The authoritative
   source is `~/Downloads/travistorrent-java-ci-build-dataset-master.zip` (5.0 GB).
4. **Most of the volume is logs, which the model cannot use.** Every `tr_log_*` field is
   post-build and therefore a forbidden input (spec §8.3). The logs only matter for analytics such as
   CI duration and test counts, and `buildlog-data-travis.csv` already contains those values in
   parsed form.

## 4. Environment findings (affect M1)

| Finding | Impact | Proposed action (you run it, or approve me running it) |
|---|---|---|
| Host disk **97% full: 7.8 GiB free**. The dataset is stored three times: zip 5.0 GB + extracted 5.3 GB + partial repo copy 2.1 GB. | Docker images for the full stack plus Parquet/MLflow artifacts will not fit. | Keep the zip as the archive of record. Extract only `*.csv`/`*.json`/`errors` (a few MB). Delete the two extracted copies (~7.4 GB freed). Deleting is your call. |
| Docker CLI context is `desktop-linux`, but Docker Desktop's daemon is not running. **Colima** is running (2 CPU / 4 GiB RAM / 20 GiB disk, 41 images ≈ 9.0 GB, 4.0 GB reclaimable). | `docker compose` fails by default. 4 GiB cannot hold Kafka + Postgres + Airflow + MLflow + the demo at the same time. | `docker context use colima`. Resize Colima to about 6 CPU / 10 GiB RAM, with a disk size that fits after cleanup. The Makefile will check the Docker context and print a clear error. |
| No GNU `timeout` on macOS. | Scripts must not assume coreutils. | Use Python or `perl -e alarm` in scripts, or document `brew install coreutils`. |
| Tooling present: Docker 29.8 CLI, Compose v5.1.3, Node 24.9, pnpm 11.1, uv 0.12.12, Python 3.12.11, gh 2.82, Go 1.27. | Everything the spec needs is installed. | None. |

## 5. Migration steps (executed in M1 after approval)

1. Commit the deletion of the root-level Next.js scaffold in its own commit:
   `chore: remove create-next-app scaffold (recreated under apps/web)`.
2. Add a root `.gitignore` covering `.venv/`, `data/raw/`, `**/.DS_Store`, `.env`, `node_modules/`,
   build outputs, MLflow/Airflow local state, and Parquet outputs.
3. Move the partial copy out of the repo, or remove it. The pipeline will read from a configurable
   `RL_DATASET_PATH` (or the zip) and never from a committed copy.
4. Scaffold the monorepo (spec §5) and commit the docs from this milestone.
