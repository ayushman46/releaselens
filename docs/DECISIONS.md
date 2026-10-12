# Architecture Decision Records

Format: **Context → Decision → Consequences**. Status values: `Proposed` (awaiting your approval),
`Accepted`, `Superseded by ADR-xxxx`. ADRs are append-only; to change a decision, write a new ADR
and mark the old one superseded.

| # | Title | Status |
|---|---|---|
| 0001 | Record decisions as ADRs in this file | Accepted |
| 0002 | Monorepo tooling: uv workspace + pnpm + Makefile, Python 3.12 | Proposed |
| 0003 | Greenfield Python implementation (no Go prototype to migrate) | Proposed |
| 0004 | Historical data source for the CI-failure model | **Proposed — needs your decision** |
| 0005 | Prediction target and label definition | Proposed |
| 0006 | Feature logic lives only in `rl_core`; dbt calls it through a Python model | Proposed |
| 0007 | Kafka in KRaft mode, topic layout and delivery semantics | Proposed |
| 0008 | Postgres (partitioned) as the span store; ClickHouse only past explicit thresholds | Proposed |
| 0009 | S3-compatible object storage for local development | Proposed |
| 0010 | Multi-tenancy enforcement (TenantContext + composite indexes + RLS) | Proposed |
| 0011 | Async work = Kafka + Postgres job table + transactional outbox (no Redis/Celery) | Proposed |
| 0012 | User-facing score: percentile risk score by default, probability only behind a calibration gate | Proposed |
| 0013 | Airflow version and dependency isolation | Proposed |
| 0014 | Version pinning policy and initial pins | Proposed |
| 0015 | Local container engine and resource budget | Proposed |

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
    `packages/rl_core`, `services/api`, `services/worker`, `ml`, `data/analysis`, `demo/services/*`.
    `rl_core` is consumed as a workspace path dependency, never copied.
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

> **Status: Proposed — needs your decision.** The options and the evidence behind them are filled
> in from the M0 dataset audit (`docs/data/DATASET_AUDIT.md`).

*(See the completed text below this table once the audit is merged; placeholder while drafting.)*

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
  - Dev: **one combined broker+controller node** (KRaft) using the official `apache/kafka` image,
    with heap capped through `KAFKA_HEAP_OPTS` (ADR-0015).
  - Topics are created by `infra/kafka/bootstrap.sh` with explicit settings. Auto-create is
    disabled.

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

> Filled from the M0 stack research: MinIO's distribution status changed in 2025, so this needs a
> verified decision. Placeholder while drafting.

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

> Pin filled from the M0 stack research (2.x vs 3.x component split and RAM). Placeholder while drafting.

## ADR-0014 — Version pinning policy and initial pins

> Filled from the M0 stack research. Placeholder while drafting.

## ADR-0015 — Local container engine and resource budget

> Filled from the M0 environment findings. Placeholder while drafting.
