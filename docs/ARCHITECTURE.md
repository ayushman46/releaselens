# ReleaseLens — Architecture

> Status: **M0 draft, pending approval.** No application code exists yet. This doc describes the
> target system; every component section names the milestone that builds it. Numbers marked
> `TBD — measured in Mx` are deliberately empty until measured.

Product loop: **PREDICT → DEPLOY → OBSERVE → DETECT → EXPLAIN → LEARN**

ReleaseLens never deploys code. It plugs into the customer's existing GitHub / CI/CD through
(1) a GitHub App + composite GitHub Action, (2) a deployment-event API, and (3) an OTLP
telemetry endpoint.

---

## 1. System context

```mermaid
flowchart LR
  subgraph Customer["Customer environment"]
    GH["GitHub repo + PRs"]
    CI["CI runner<br/>(GitHub Actions)"]
    CD["CD pipeline"]
    SVC["Customer services<br/>(OTel SDK)"]
    COL["OTel Collector<br/>(customer-run)"]
  end

  subgraph RL["ReleaseLens"]
    WEB["Web app<br/>(Next.js)"]
    API["API<br/>(FastAPI)"]
  end

  DEV(["Engineer"])
  SLACK["Slack / Email"]
  STRIPE["Stripe (test mode)"]

  GH -- "webhooks (HMAC)" --> API
  CI -- "ReleaseLens Action:<br/>change facts → risk" --> API
  API -- "Check Run" --> GH
  CD -- "POST /api/v1/deployments" --> API
  SVC --> COL -- "OTLP/HTTP /v1/traces<br/>x-releaselens-key" --> API
  API -- "alerts" --> SLACK
  API <-- "checkout / webhooks" --> STRIPE
  DEV --> WEB --> API
```

## 2. Containers (runtime components)

```mermaid
flowchart TB
  subgraph core["Compose profile: core"]
    WEB["web<br/>Next.js App Router"]
    API["api<br/>FastAPI · auth · risk inference · ingest · webhooks"]
    WRK["worker<br/>Kafka consumers + Postgres job runner + outbox relay"]
    PG[("PostgreSQL 16<br/>OLTP + spans (partitioned) + analytics_*")]
    KF[["Kafka (KRaft, 1 broker in dev)"]]
    MP["mailpit<br/>(SMTP sink)"]
  end
  subgraph data["Compose profile: data"]
    S3[("S3-compatible object store<br/>(MinIO locally — see ADR-0009)")]
    AF["Airflow<br/>(own image, LocalExecutor)"]
    DBT["dbt-duckdb<br/>(invoked by Airflow)"]
    MLF["MLflow<br/>tracking + registry"]
  end
  subgraph demo["Compose profile: demo"]
    DS["gateway · orders · inventory · payment · fraud<br/>(FastAPI + OTel)"]
    OC["otel-collector (contrib)"]
    K6["k6"]
  end

  WEB --> API
  API --> PG
  API --> KF
  WRK --> KF
  WRK --> PG
  WRK --> MP
  API -- "load champion model" --> MLF
  MLF --> PG
  MLF --> S3
  AF --> DBT --> S3
  AF --> PG
  AF -- "train / register" --> MLF
  K6 --> DS --> OC -- "OTLP/HTTP" --> API
```

### Component responsibilities

| Component | Owns | Does NOT own | Milestone |
|---|---|---|---|
| `apps/web` | Marketing site, docs (MDX), authenticated dashboard; typed client generated from the API's OpenAPI | Any business logic or direct DB access | M11 (skeleton M1) |
| `services/api` | REST API, auth/RBAC, API keys, tenant resolution, risk inference (champion model + SHAP), deployment-event intake → Kafka, OTLP ingest → Kafka, GitHub + Stripe webhooks, outbox writes | Long-running analysis, consuming Kafka | M1, M5, M6, M7, M10 |
| `services/worker` | Kafka consumers (deployments, spans, analysis results → notifications), Postgres job runner (`FOR UPDATE SKIP LOCKED`), outbox relay, retention jobs | HTTP endpoints (only a health endpoint) | M6–M10 |
| `packages/rl_core` | **Single source of truth**: domain models, event schemas, feature definitions (`FEATURE_SCHEMA_VERSION`), regression engine, self-time, service graph, triage/origin scoring, blast radius, severity | I/O (no DB/Kafka/HTTP clients) — pure functions + typed models | M2 onward |
| `ml/` | Training, evaluation, calibration, SHAP, model card, MLflow logging + registration | Serving (API loads the model) | M4 |
| `data/airflow` | DAG orchestration of the historical + outcome-export pipelines | Transform logic (lives in dbt / rl_core) | M2–M4 |
| `data/dbt` | staging → intermediate → marts → ml models and their tests | Feature formulas (calls `rl_core`, see ADR-0006) | M3–M4 |
| `data/analysis` | Reproducible statistical analysis → `FINDINGS.md` + JSON | — | M3 |
| `demo/` | Five instrumented services with a deterministic, env-controlled regression; k6 traffic; scripted scenarios | — | M7–M9 |
| `integrations/github-action` | Composite Action that computes repo-local change facts in the runner and calls the API | — | M5 |
| `infra/` | Compose files + profiles, OTel Collector configs, Kafka topic bootstrap | — | M1 |

## 3. Offline / historical path (M2–M4)

```mermaid
flowchart LR
  RAW["Historical CI dataset<br/>(source: ADR-0004)"] --> V["validate_raw_schema"]
  V --> PQ["convert_to_parquet"] --> S3R[("raw/…/version=v")]
  S3R --> STG["dbt staging<br/>(dedupe jobs→builds)"] --> INT["dbt intermediate<br/>(history, change profile, timing)"]
  INT --> MARTS["dbt marts<br/>(fct_builds, dim_*, mart_*)"] --> S3M[("marts/…/run_id=")]
  MARTS --> FEAT["ml/mart_build_features<br/>(dbt Python model → rl_core.features)"] --> S3F[("features/…/schema=/run_id=")]
  MARTS --> EXP["export_analytics_to_postgres"] --> PGA[("Postgres analytics_*")]
  S3F --> TRAIN["ml/train → evaluate → calibrate → SHAP"]
  TRAIN --> REG["MLflow registry<br/>release_risk_ci@challenger"]
  REG --> GATE{"promotion gate<br/>PR-AUC ≥ champion ∧ leakage tests ∧ model card"}
  GATE -- pass --> CHAMP["@champion"]
```

Every task is idempotent: object paths are keyed by dataset version / `run_id`, Postgres exports
use `INSERT … ON CONFLICT` or swap-table loads, and data-quality gates fail the DAG.

## 4. Live pre-deployment path (M5)

```mermaid
sequenceDiagram
  autonumber
  participant GH as GitHub
  participant ACT as ReleaseLens Action (in CI runner)
  participant API as ReleaseLens API
  participant DB as Postgres
  participant M as champion model (in-process)

  GH->>API: pull_request / check_suite webhook (X-Hub-Signature-256)
  API->>DB: webhook_deliveries (dedupe by delivery id)
  ACT->>ACT: compute change facts (churn src/test, files, tests ±, sloc)
  ACT->>API: POST /api/v1/risk/assessments (API key, scope risk:write)
  API->>DB: load tenant build history (ci_runs, strictly finished before now)
  API->>M: rl_core.features.online → feature vector (FEATURE_SCHEMA_VERSION)
  M-->>API: raw score + SHAP values
  API->>DB: risk_assessments (features, score, band, shap, model_version)
  API->>GH: Check Run "Risk HIGH · 78/100 · top signals…"
  API-->>ACT: assessment_id, risk_score, band, top_increasing/reducing
```

CI pass/fail and CI duration-vs-normal are displayed **next to** the risk score as separate
signals, never as model inputs (they are, or leak, the label).

## 5. Live post-deployment path (M6–M9)

```mermaid
sequenceDiagram
  autonumber
  participant CD as CD pipeline
  participant API as API
  participant K as Kafka
  participant W as worker
  participant DB as Postgres
  participant OC as OTel Collector

  CD->>API: POST /api/v1/deployments (deployments:write)
  API->>K: deployment.events key=tenant_id:service (envelope v1/v2)
  API-->>CD: 202 {event_id}
  K->>W: consume (manual commit)
  W->>DB: INSERT deployments ON CONFLICT (tenant_id,event_id) DO NOTHING<br/>+ analysis_jobs(run_at = T+5, T+10, T+15 min)
  W->>K: commit offset (after DB commit)
  OC->>API: POST /v1/traces (OTLP proto/JSON, x-releaselens-key)
  API->>K: telemetry.spans key=tenant_id:service.name
  K->>W: consume spans
  W->>DB: spans (daily partitions) + service_edges + service_minute_rollups
  W->>DB: due job? SELECT … FOR UPDATE SKIP LOCKED
  W->>W: rl_core: regression gates → self-time → graph → probable origin → blast radius
  W->>DB: regression_analyses + outbox row (same transaction)
  W->>K: outbox relay → analysis.results
  K->>W: notifier → Slack / email (notification_log)
```

### Kafka topics

| Topic | Key | Producer | Consumer(s) | Partitions / retention (dev) |
|---|---|---|---|---|
| `deployment.events` | `tenant_id:service` | api | worker (deployments) | set by bootstrap script — values in ADR-0007 |
| `telemetry.spans` | `tenant_id:service.name` | api (OTLP ingest) | worker (spans) | ″ |
| `analysis.results` | `tenant_id:deployment_id` | worker (outbox relay) | worker (notifier) | ″ |
| `*.dlq` | original key | consumers on permanent error | operator tooling | ″ |

Delivery semantics: at-least-once + idempotent persistence (`UNIQUE(tenant_id, event_id)`).
Transient errors → bounded exponential backoff with jitter, then partition pause/resume.
Permanent errors → DLQ with reason, stack, original topic/partition/offset.

**As built (M7/M8)** — details and limits in [`docs/events/TELEMETRY.md`](events/TELEMETRY.md):

* The API answers `/v1/traces` only after Kafka acknowledged every message (one per service per
  request, split above 768 KiB); rejected spans are reported through OTLP `partial_success`.
* The span consumer handles up to 100 messages per transaction; spans are idempotent on
  `(tenant_id, trace_id, span_id, start_time)`, and edges and minute rollups are derived only
  from the rows that transaction inserted. A per-tenant advisory lock makes edge counts exact
  when a parent and its child are stored by concurrent consumers.
* Day partitions are created by the worker's maintenance loop (today + 2 days, hourly) and, for
  any other day, by the consumer before its insert transaction; retention drops partitions
  (7 days) and deletes rollups (30 days).
* Analysis jobs: `early_5m` / `early_10m` analyse **as of** T+5 / T+10 even when they run late;
  `final` runs at T+15 on the full window. Window profiles are per environment (`demo`: baseline
  10 min, warm-up 1 min, post window to T+8). The handler writes `regression_analyses` and the
  `analysis.results` outbox row (`rl_core.events.analysis.AnalysisEnvelope`) in one transaction.
* `spans` has no foreign key to `organizations` (per-row cost on the hottest path); the consumer
  checks each batch's tenants once and dead-letters unknown ones.

## 6. Post-deployment intelligence (all in `rl_core`, M8–M9)

```mermaid
flowchart LR
  W["windows<br/>baseline [T−30m,T)<br/>post [T+2m,T+15m]"] --> G["per-service gates<br/>sample ≥ min_requests<br/>latency: rel ≥25% ∧ abs ≥20ms ∧ bootstrap CI ∌ 0<br/>errors: z-test p<0.01 ∧ Fisher exact p<0.01 ∧ +0.5pp ∧ +50%<br/>(ADR-0018)"]
  G --> ST["self-time per span<br/>(union of child intervals)"]
  ST --> GR["service graph from traces<br/>(cycles, diamonds, multi-root)"]
  GR --> OR["probable-origin scoring<br/>(weighted evidence table)"]
  OR --> BR["blast radius<br/>BFS up/down + own-telemetry check"]
  BR --> OUT["regression_analyses row<br/>(evidence JSON, engine version, windows)"]
```

Wording rule: **"probable origin"**, never "root cause". Severity v1 is a deterministic score,
not a probability.

## 7. Feedback path (M4 monitoring, M10+)

```mermaid
flowchart LR
  RA["risk_assessments<br/>(prediction)"] --> J["join on deployment / commit"]
  RAN["regression_analyses<br/>(observed outcome)"] --> J
  CIR["ci_runs<br/>(observed CI outcome)"] --> J
  J --> DAG["Airflow outcome_export"] --> S3O[("outcomes/releaselens/dt=")] --> DBT2["dbt"] --> FUT["future model:<br/>release_outcome_v1"]
```

## 8. Data stores

| Store | Holds | Tenancy |
|---|---|---|
| Postgres (app DB) | OLTP tables, spans (declaratively partitioned by day), edges, minute rollups, jobs, outbox, analytics_* | `tenant_id NOT NULL` on every tenant-owned table; composite indexes lead with `tenant_id`; optional RLS (ADR-0010) |
| Postgres (MLflow / Airflow metadata) | Separate **databases** on the same server in dev | n/a (internal) |
| Object store `releaselens-data` | raw/, marts/, features/, outcomes/, reports/ (Parquet/JSON) | historical data is not tenant data; `outcomes/` partitioned by tenant |
| Object store `releaselens-mlflow` | MLflow artifacts | n/a |
| Kafka | Event log, short retention | `tenant_id` in key **and** payload |

Proposed addition to the spec's table list (needed for online history features):
**`ci_runs`** (tenant_id, project_id, repo, commit_sha, branch, is_pr, started_at, finished_at,
status, duration_s, source) populated from `check_suite` / `workflow_run` webhooks and an
optional backfill on GitHub App installation.

## 9. `rl_core` module map (planned)

```
rl_core/
  tenancy.py          TenantContext (required by every repository function)
  events/             versioned envelopes (deployment.completed v1, v2), span models
  features/
    schema.py         FEATURE_SCHEMA_VERSION, feature list, FORBIDDEN_INPUTS
    history.py        strictly-finished-before history windows (pure, sorted per project)
    change.py         change-size / test / codebase features
    offline.py        vectorised (Polars) batch computation — used by the dbt Python model
    online.py         per-request computation — used by the API
  regression/         windows, percentile bootstrap, two-proportion z-test, gates, severity v1
  selftime.py         interval-union self-time
  graph.py            service graph + cycle-safe BFS/DFS
  triage.py           probable-origin scoring + reasoning text
  blast_radius.py
```

## 10. Cross-cutting concerns

- **Multi-tenancy:** API key or session → `TenantContext`; repositories refuse calls without it;
  Kafka keys/payloads carry `tenant_id`; cross-tenant tests are mandatory (M6, M10).
- **Idempotency:** `(tenant_id, event_id)` for deployment events, GitHub delivery IDs, Stripe
  event IDs, outbox row IDs.
- **Self-observability:** structured JSON logs with trace/span IDs from day one (M1);
  ReleaseLens' own services can emit OTel to the same collector in dev.
- **Security:** Argon2id, hashed API keys with visible prefix, CSRF for cookie auth, strict CORS,
  signature-verified webhooks, rate limits on auth + ingest, secrets only via env files.
- **Honesty guardrails:** "probable origin"; risk score 0–100 unless calibration justifies a
  probability; the CI-failure model is never described as an outage predictor.

## 11. Deployment topology & resource budget (dev machine)

Target dev machine: Apple M4, 10 cores, 16 GB RAM. Container engine: Colima. Per-profile memory
and disk figures: `TBD — measured in M1`. See `docs/PLAN.md` § Environment constraints.
