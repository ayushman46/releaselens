# Architecture Decision Records

Format: **Context → Decision → Consequences**. Status values: `Proposed` (awaiting approval; in overnight mode decisions are taken and listed in `QUESTIONS_FOR_OWNER.md`),
`Accepted`, `Superseded by ADR-xxxx`. ADRs are append-only; to change a decision, write a new ADR
and mark the old one superseded.

| # | Title | Status |
|---|---|---|
| 0001 | Record decisions as ADRs in this file | Accepted |
| 0002 | Monorepo tooling: uv workspace + pnpm + Makefile, Python 3.12 | Accepted |
| 0003 | Greenfield Python implementation (no Go prototype to migrate) | Accepted |
| 0004 | Historical data source: Figshare TravisTorrent (CC BY 4.0), Java subset | Accepted (overnight; see QUESTIONS_FOR_OWNER) |
| 0005 | Prediction target and label definition | Accepted |
| 0006 | Feature logic lives only in `rl_core`; dbt calls it through a Python model | Accepted |
| 0007 | Kafka in KRaft mode, topic layout and delivery semantics | Accepted |
| 0008 | Postgres (partitioned) as the span store; ClickHouse only past explicit thresholds | Accepted |
| 0009 | S3-compatible object storage: pgsty/silo (MinIO fork) since MinIO images are gone | Accepted |
| 0010 | Multi-tenancy enforcement (TenantContext + composite indexes + RLS) | Accepted |
| 0011 | Async work = Kafka + Postgres job table + transactional outbox (no Redis/Celery) | Accepted |
| 0012 | User-facing score: percentile risk score by default, probability only behind a calibration gate | Accepted |
| 0013 | Airflow 3.3.2 in its own venv; tasks via external_python | Accepted |
| 0014 | Version pinning policy and initial pins | Accepted |
| 0015 | Local container engine (Colima) and resource budget | Accepted |

---

## ADR-0001 — Record decisions as ADRs in this file

- **Context.** The spec requires an ADR for every technology outside Section 4 and for several
  in-scope choices (label definition, Airflow pin, span storage, RLS).
- **Decision.** All ADRs live in this single file, numbered sequentially, using the format above.
- **Consequences.** One place to review. If the file grows unwieldy, split it into
  `docs/adr/NNNN-*.md` files and keep this file as the index.

## ADR-0002 — Monorepo tooling: uv workspace + pnpm + Makefile, Python 3.12

- **Context.** Five Python deployables or libraries (`rl_core`, `api`, `worker`, `ml`,
  `data/analysis`) must share `rl_core` with zero drift. The web app is TypeScript. Airflow has its
  own strict dependency constraints (ADR-0013).
- **Decision.**
  - A single **uv workspace** at the repo root, with one shared lockfile (`uv.lock`). Members:
    `packages/rl_core` (pure logic), `packages/rl_db` (SQLAlchemy models, repositories, Alembic,
    shared by API and worker, so `rl_core` stays I/O-free), `services/api`, `services/worker`,
    `ml`, `data/pipeline` (Airflow-agnostic task callables), `data/analysis`, and `demo/services`.
    Members are added as their milestone starts. `rl_core` is consumed as a workspace
    dependency, never copied.
  - **pnpm** for `apps/web` and `integrations/github-action` (if it needs JS).
  - A **Makefile** is the single entry point: `up-core`, `up-data`, `up-demo`, `test`, `lint`,
    `seed`, `demo`. Under the hood it calls `uv`, `pnpm` and `docker compose`.
  - Python **3.12** everywhere: it is the spec's version, the installed interpreter, and is
    supported by the whole ML stack (verified in ADR-0014).
  - Lint and type tooling: `ruff` (lint + format) and `mypy --strict` on `rl_core`. `mypy` runs
    non-strict on services at first and is tightened per milestone.
- **Consequences.** One lockfile means one resolution for the API, the worker and training, which
  is what training/serving parity requires. Airflow is deliberately **outside** this lockfile.

## ADR-0003 — Greenfield Python implementation

- **Context.** The repository holds only a Create Next App scaffold, uncommitted deletions, raw
  data and empty folders. The Go + Kafka + Postgres + ClickHouse prototype described in the brief
  is not present (search details are in `MIGRATION.md` §1.3).
- **Decision.** Start fresh in the spec's stack. The prototype's *concepts* carry forward: the
  envelope schema, `(tenant_id, event_id)` idempotency, and the duplicate-delivery test
  scenarios. If you later provide the prototype, it gets audited against `MIGRATION.md` §2 before
  M6 starts.
- **Consequences.** No migration risk, and no tested code to reuse either. M6's test list is
  written from the spec.

## ADR-0004 — Historical data source for the CI-failure model

> **Status: Accepted (autonomous overnight decision; listed in `QUESTIONS_FOR_OWNER.md`).**

- **Context.** The spec assumes the TravisTorrent 2017 build table with `gh_*` change features,
  "Java subset ≈ 519K builds across ~20 projects". The M0 audit (`docs/data/DATASET_AUDIT.md`)
  established three things:
  1. The archive on disk is `monperrus/travistorrent-java-ci-build-dataset`: raw travistorrent-tools
     harvester output with **no `gh_*` columns**. It has 64,736 labelled builds, and its "519,373
     Java builds" description cannot be reproduced. It carries no license.
  2. The official `travistorrent_8_2_2017` dump is **unobtainable**. `travistorrent.testroots.org`
     now 301-redirects to an unrelated APK site (never fetch from it), BigQuery access was retired,
     and Wayback has no copy of the dump files.
  3. The TravisTorrent authors re-published their build table on **Figshare**
     (DOI 10.6084/m9.figshare.19314170, **CC BY 4.0**): `final-2017-01-25.csv.gz`, 264,607,174 bytes,
     md5 `5d359345fecf0bf47be6edb10b5aae58`. It is live (HTTP 206 on a range probe), it is the
     authors' own data, and it carries the change, test, codebase and team columns under their
     2017 names (`git_diff_src_churn`, `gh_diff_files_added`, …). **Measured after download
     (md5 verified, DuckDB stream, 27 s):** 66 columns, 3,881,992 job rows, **925,897 builds**,
     948 projects. By `gh_lang`: ruby 273,220 builds (248 projects), python 338,176 (261),
     **java 200,561 builds (241 projects)**, go 113,940 (198).
- **Decision.**
  - **Primary training and analytics source: Figshare `final-2017-01-25.csv.gz`.** Use the
    **Java subset** (`gh_lang = 'java'`), deduplicated to one row per `tr_build_id`. Real
    row/project counts are reported in M2. The spec's 519K figure is treated as unverified, and
    the true number replaces it.
  - The pipeline **streams** the `.csv.gz` with DuckDB straight to Parquet and never decompresses
    it to disk (about 3.5 GB uncompressed against about 9 GiB free). The md5 is verified on every
    run, and the dataset version is that md5.
  - **Secondary source: the on-disk monperrus archive**, used for (a) cross-validation (join on
    `tr_build_id` and report the status agreement rate) and (b) log-derived analytics. It is never
    the training source, because it has no change features and no license.
  - **Online feature extraction (M5)** computes change facts from git in the customer's CI runner,
    following TravisTorrent's definitions as closely as git allows (`DATASET_AUDIT.md` §6; full
    definitions in `docs/data/DATA_DICTIONARY.md`). Where git cannot reproduce a column (for
    example `gh_num_commits_in_push`, which came from GHTorrent push events), the feature is
    **excluded from the model** rather than approximated differently online and offline.
    Feature-extraction fidelity against TravisTorrent's values is measured on cloned repos in M5
    and reported.
  - **Attribution:** CC BY 4.0 requires credit. The README and the model card cite Beller,
    Gousios & Zaidman, *TravisTorrent*, MSR 2017, DOI 10.1109/MSR.2017.24, and the Figshare DOI.
- **Consequences.**
  - Training uses real TravisTorrent build data with the spec's feature families.
  - Columns found empty (reportedly the four push-level `gh_*` fields) are documented and dropped.
  - Data ends in Aug 2016. The model card states that it predicts CI failure for 2012–2016
    Travis-era open-source projects, and that transfer to a 2026 customer's GitHub Actions is
    unproven. The cross-project and cold-start evaluations (spec §8.4) quantify the risk that
    can be quantified.
  - The data is a re-publication (`final-2017-01-25`), not byte-identical to the MSR 2017
    `8_2_2017` release. Row counts will differ from papers that used `8_2_2017`.

## ADR-0005 — Prediction target and label definition

- **Context.** The spec defines the target as build/CI failure, predicted at the moment a build is
  triggered. Travis statuses are `passed`, `failed`, `errored` and `canceled`. In Travis, `errored`
  means the build broke **before or outside** the test script (install/setup/infrastructure), and
  `failed` means the script itself failed.
- **Decision.**
  - `label = 1` if `status ∈ {failed, errored}`; `label = 0` if `status = passed`; **exclude
    `canceled`** (no outcome was observed). Report the class balance from real data.
  - **Sensitivity analysis (added):** also train and evaluate with `errored` excluded, and report
    both. `errored` is often infrastructure noise that pre-build change features cannot explain,
    so we need to know how much it moves PR-AUC.
  - Product framing everywhere: *"Build/CI failure risk from historical patterns."* Never "outage
    probability".
- **Consequences.** The label mixes code and infrastructure failures, and the model card has to
  say so. The `errored`-excluded variant is a candidate model if it is materially better and the
  product framing still holds.

## ADR-0006 — Feature logic lives only in `rl_core`; dbt calls it through a Python model

- **Context.** The spec wants (a) `rl_core` to be the single source of truth for feature
  definitions and (b) a dbt `ml/mart_build_features` table. Writing the history features as dbt SQL
  window functions *and* as Python for online inference gives two implementations, which will
  drift over time.
- **Decision.**
  - dbt models up to `marts/` (plus `ml/mart_build_inputs`: per-build pre-build facts and the build
    timeline `started_at`, `finished_at` or the upper bound `started_at + duration`) stay in SQL.
  - `ml/mart_build_features` is a **dbt-duckdb Python model** that imports
    `rl_core.features.offline` (vectorised Polars). The API uses `rl_core.features.online`, which
    computes per row through the same pure functions.
  - **Parity test:** for a fixed sample of historical builds, the offline batch output and the
    row-by-row online path must be identical, compared column by column with exact equality for
    integer and boolean columns and a stated `rtol` for floats.
  - Leakage tests run in two places: dbt custom tests on `mart_build_inputs`/`mart_build_features`,
    and `ml/tests/test_leakage.py` on the Python functions.
- **Consequences.** dbt keeps lineage and tests. Feature formulas exist exactly once. The dbt
  runtime image must install `rl_core`, which the uv workspace already provides.

## ADR-0007 — Kafka in KRaft mode, topic layout, delivery semantics

- **Context.** The spec requires KRaft, explicit topic configuration, at-least-once delivery and a DLQ.
- **Decision.**
  - Dev and tests: **one combined broker+controller node** (KRaft) on
    `apache/kafka-native:4.3.1`. This is the official GraalVM build, intended for dev/test:
    55 MB image, about 260 MiB RSS measured. Production guidance is the JVM `apache/kafka`
    image with replication ≥ 3.
  - Topics are created by `rl_worker.kafka_admin` (run by the compose `kafka-init` job and by
    `infra/kafka/bootstrap.sh`), with explicit partitions, retention and cleanup policy.
    Broker auto-create is disabled. The native image has no Kafka CLI, and one Python
    definition keeps tests and runtime identical.

    | Topic | Partitions (dev) | Retention | Cleanup |
    |---|---|---|---|
    | `deployment.events` | 6 | 7 d | delete |
    | `telemetry.spans` | 12 | 24 h | delete |
    | `analysis.results` | 6 | 7 d | delete |
    | `deployment.events.dlq`, `telemetry.spans.dlq`, `analysis.results.dlq` | 1 | 30 d | delete |

    Partition counts are dev defaults chosen for consumer parallelism headroom, not measurements.
  - Keys: `tenant_id:service` (deployments) and `tenant_id:service.name` (spans), which keeps
    per-service order. Payloads always repeat `tenant_id`.
  - Producers: `acks=all`, `enable.idempotence=true`. Consumers: `enable.auto.commit=false`, with
    the offset committed after the Postgres transaction commits.
- **Consequences.** A single broker gives no durability guarantees in dev. That is acceptable
  locally, and production replication settings are documented separately. At-least-once delivery
  is safe only because persistence is idempotent, and M6 tests that property.

## ADR-0008 — Postgres (partitioned) as the span store

- **Context.** ClickHouse is excluded unless justified. Demo-scale span volume is modest
  (`TBD — measured in M7`).
- **Decision.** Store spans in a Postgres table **declaratively partitioned by day**
  (`start_ns`-derived), with indexes `(tenant_id, service, environment, start_ns)` and
  `(tenant_id, trace_id)`. Writes use batched `COPY`/multi-row inserts from the span consumer.
  Retention works by dropping whole partitions per plan (7 or 30 days). Minute rollups are
  pre-aggregated for UI charts.
- **Revisit thresholds** (measured, not guessed): if sustained ingest exceeds the measured
  single-node `COPY` capacity at 50% headroom, **or** p95 latency of the regression engine's window
  query exceeds 2 s at the target tenant scale, write an ADR evaluating ClickHouse. Both thresholds
  are measured with k6 in M12.
- **Consequences.** One database to operate. Percentiles are computed in Python (`rl_core`) from
  raw spans in the windows, which is fine at demo scale and the first bottleneck at large scale.

## ADR-0009 — S3-compatible object storage for local development

- **Context.** Spec §2.8 says "MinIO locally". As of 2026-10 MinIO is not viable. The upstream repo
  is archived ("NO LONGER MAINTAINED"), and the community edition is source-only. `minio/minio`
  and `minio/mc` cannot be pulled (Docker Hub: "object not found"; quay.io: 401, measured
  2026-10-12). Bitnami's free images are gone. LocalStack now requires an auth token.
- **Options.**
  - SeaweedFS 4.48 (Apache-2.0, ~184 MB image).
  - Garage v2.4.1 (AGPL-3.0, ~27 MB, but needs a manual layout/key bootstrap and lacks
    versioning and bucket policies).
  - **pgsty/silo** (AGPL-3.0 community fork of MinIO, ~52 MB, MinIO env vars and console, monthly
    releases, small maintainer).
- **Decision.** Use **`pgsty/silo:RELEASE.2026-09-16T00-00-00Z`** as the local S3 server. It is
  the closest thing to "MinIO locally" (same `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`, same API
  and console) and the smallest image that keeps a shell for healthchecks. The compose service is
  still called `minio` so docs and the spec read naturally. Buckets are created by a Python
  (`boto3`) init job from our own image, so no `mc` image is needed. All clients use
  `RL_S3_ENDPOINT` with path-style addressing, so swapping in SeaweedFS (the documented fallback)
  or real AWS S3 is a config change. **AGPL note:** we only *run* it as a separate dev process and
  never link or distribute it, so the AGPL does not attach to ReleaseLens code.
- **Consequences.** There is a bus-factor risk in a small fork, acceptable for dev only.
  Production would use AWS S3 (owner-provisioned, `BLOCKERS.md`).

## ADR-0010 — Multi-tenancy enforcement

- **Context.** Cross-tenant leaks are the most damaging bug class for this product.
- **Decision.**
  1. Every tenant-owned table has `tenant_id NOT NULL`, and every index on it leads with `tenant_id`.
  2. Repository functions take a mandatory `TenantContext` argument. There are no
     "global" query helpers outside an explicit `admin` module.
  3. **Postgres Row-Level Security as defence in depth** (M10): the app connects as a
     non-owner role. Each transaction sets `SET LOCAL app.tenant_id = …`, and policies compare
     against `current_setting('app.tenant_id')`. Workers that legitimately span tenants (retention)
     use a separate role with `BYPASSRLS`, and audit logging records those calls.
  4. Cross-tenant tests: every read and write endpoint is exercised with tenant B's credentials
     against tenant A's IDs and must return 404 (resource existence is never leaked).
- **Consequences.** Some extra boilerplate per query. RLS adds a second, independent guard against
  a missed `WHERE tenant_id = …`.

## ADR-0011 — Async work without Redis/Celery

- **Context.** We need scheduled analysis jobs (+5, +10 and +15 min after a deploy), event
  consumption and reliable publication of results.
- **Decision.** Kafka carries event streams. A Postgres `analysis_jobs` table, polled with
  `SELECT … FOR UPDATE SKIP LOCKED` and carrying `run_at`, `attempts` and `locked_by`, provides
  delayed and retryable jobs. A transactional **outbox** table, relayed to Kafka by the worker,
  publishes `analysis.results` atomically with the DB write.
- **Consequences.** Two fewer moving parts. Job throughput is bounded by Postgres, which is ample
  for per-deployment jobs.

## ADR-0012 — User-facing score

- **Context.** Showing a raw model output as "probability" is only honest if it is calibrated on
  data the model never saw.
- **Decision.** Default output is `risk_score` = percentile of the raw score among
  **validation-set** predictions (0–100). Risk bands are derived from validation operating points
  and stored in the model's registry metadata. `probability` is exposed **only if** the calibrated
  model (isotonic or Platt, fit on validation) meets **all** of these on the untouched test set,
  thresholds fixed now and before seeing results:
  - expected calibration error (10 equal-mass bins) ≤ 0.05;
  - Brier skill score vs. the base-rate predictor > 0;
  - calibration slope (logistic recalibration) in [0.8, 1.2].
- **Consequences.** Users may see only a score, and the UI copy explains what the percentile
  means. Writing the criteria down before seeing results prevents post-hoc justification.

## ADR-0013 — Airflow version and dependency isolation

- **Context.** Airflow 2.x reached EOL on 2026-04-22. Airflow **3.3.2** (2026-09-17) is current.
  Its constraints pin `fastapi<0.137`, `sqlalchemy 2.0.52`, `pydantic 2.13.5` and others, which
  conflicts with our workspace (fastapi 0.143, SQLAlchemy 2.1). Airflow 3 runs as separate
  `api-server`, `scheduler`, `dag-processor` and `triggerer` processes, and its docs recommend at
  least 4 GB RAM. The Colima VM has 4 GiB total, shared with the core stack.
- **Decision.**
  - Pin **`apache-airflow==3.3.2`**, installed **with the official constraints file**
    (`constraints-3.3.2/constraints-3.12.txt`) into its **own virtualenv** (`/opt/airflow-venv`)
    in an `rlens/airflow` image built `FROM rlens/python` (shared base layers, no 660 MB upstream
    image).
  - DAG tasks run our code in the **workspace venv** via `@task.external_python(python=
    "/app/.venv/bin/python")`. Task functions are thin wrappers over `rl_pipeline.tasks.*`,
    which are plain Python callables, unit-testable without Airflow.
  - Executor: **LocalExecutor**. Metadata DB: the `airflow` database on the shared Postgres.
  - **Verification on this machine** uses `airflow dags test historical_pipeline` (one process,
    real task code, real metadata DB), which the spec allows ("Airflow DAG (or task-level
    runner)"). The `data` profile also defines the full four-process Airflow deployment for
    machines with ≥ 8 GiB available to Docker.
- **Consequences.** Two Python environments to maintain, but they never conflict. Airflow
  version bumps are isolated.

## ADR-0014 — Version pinning policy and initial pins

- **Policy.**
  1. Python dependencies are locked **exactly** in `uv.lock`. `pyproject.toml` holds lower bounds,
     plus upper bounds only where a known break exists.
  2. Container images are pinned to an exact version tag. Floating tags (`latest`, `16-alpine`)
     are never used.
  3. JS dependencies are locked in `pnpm-lock.yaml`, with exact versions for framework packages.
  4. Security tools are pinned by exact version. Trivy is pinned **by digest** and
     `trivy-action` **by commit SHA**, because of the 2026-03 Trivy supply-chain compromise
     (CVE-2026-33634: never 0.69.4–0.69.6).
  5. Upgrades are deliberate commits that run the full test matrix.
- **Initial pins (2026-10-13):**

  | Component | Pin | Why / note |
  |---|---|---|
  | Python | 3.12 (`python:3.12-slim`, cached locally) | Spec version. Floor for xgboost ≥ 3.3, shap ≥ 0.52, numpy ≥ 2.5 |
  | PostgreSQL | `postgres:16.15-alpine` | Spec major. EOL 2028-11-09 |
  | Kafka | `apache/kafka-native:4.3.1` (dev/test) | KRaft-only 4.x line. Native image: 55 MB, ~260 MiB RAM measured |
  | confluent-kafka | 2.16.0 (lock) | — |
  | FastAPI / Starlette / Pydantic / SQLAlchemy | 0.143.0 / 1.7.0 / 2.14.1 / 2.1.4 (lock) | FastAPI's native OTel stays **off** (`FASTAPI_OTEL_AUTO_CONFIGURE` unset); we use `opentelemetry-instrumentation-fastapi` |
  | OpenTelemetry SDK | 1.45.1 (lock) | Contrib instrumentation on the matching 0.66bX train |
  | Airflow | 3.3.2 + constraints, own venv | ADR-0013 |
  | dbt-core / dbt-duckdb / duckdb | 1.12.5 / 1.11.0 / **1.5.6** | duckdb pinned explicitly: dbt-duckdb has no upper bound, and DuckDB 2.0 is due ~2026-10-21 |
  | MLflow | 3.16.1 | 3.17.0 needs a stop-the-world DB upgrade and `pyarrow<26` |
  | XGBoost | `xgboost-cpu` 3.4.1 in Linux images, `xgboost` 3.4.1 on macOS | Avoids a 305 MB NCCL wheel on Linux |
  | SHAP | ≥ 0.51 (needed for XGBoost 3.x `base_score`) | Exact pin set in M4 after the compatibility test |
  | Object store | `pgsty/silo:RELEASE.2026-09-16T00-00-00Z` | ADR-0009 |
  | OTel Collector | `otel/opentelemetry-collector-contrib:0.161.0` | Receivers must bind `0.0.0.0` explicitly |
  | k6 | `grafana/k6:2.3.0` | — |
  | Mailpit | `axllent/mailpit:v1.31.3` | — |
  | gitleaks | `zricethezav/gitleaks:v8.30.1` | — |
  | Next.js / React | 16.3.8 / 19.2.8 | Versions from the project's original scaffold |
  | TypeScript | 5.9.x | `openapi-typescript` needs `^5`. TS 6/7 exist but are not adopted |
  | Node (images) | `node:22-alpine` (cached) | Vitest 5 needs Node ^22.12 or 24 |

- **Consequences.** Reproducible builds. Recently released versions (FastAPI 0.143.0,
  SQLAlchemy 2.1.4, Pydantic 2.14.1) are accepted because the lockfile pins them and the test
  suite covers them. Any regression is a lockfile rollback.

## ADR-0015 — Local container engine and resource budget

- **Context.** The dev machine runs **Colima** with 2 CPU / 4 GiB RAM / 20 GiB VM disk. The Docker
  CLI default context `desktop-linux` points at a stopped Docker Desktop. The host disk has about
  9 GiB free. The host also runs unrelated containers, including the old prototype on host ports
  5432/8000 under compose project `releaselens`.
- **Decision.**
  - The Makefile selects `DOCKER_CONTEXT=colima` automatically when that context exists.
    testcontainers gets `DOCKER_HOST` from the context plus `TESTCONTAINERS_RYUK_DISABLED=true`.
  - Compose project name **`rlens`**. Host ports in a `2xxxx` block (25432, 29092, 28000, 28001,
    23000, 29000/29001, 25000, 28080, 24317/24318, 28100), all overridable in `.env`.
  - Every service has a `mem_limit`. Measured idle RSS of the core backend (2026-10-13):
    kafka-native 261 MiB, api 69 MiB, worker 47 MiB, postgres 24 MiB.
  - One shared Python image (`rlens/python`) for every Python process. Images already cached
    locally are reused wherever the pins allow.
  - `make doctor` refuses heavy targets when free disk is below 2 GiB.
  - **Recommended owner action (not done automatically, since it would restart the user's
    other containers):** `colima stop && colima start --cpu 6 --memory 10`, so the `data` and
    `demo` profiles can run alongside `core`.
- **Consequences.** The core profile fits comfortably in 4 GiB. The data and demo profiles are
  started selectively on this machine.
