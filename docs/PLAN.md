# ReleaseLens — Plan

> Living document. Updated at the end of every milestone.
> Mode: **autonomous overnight** (spec §19). Decisions that would normally wait for owner approval
> are taken, recorded as ADRs, and listed in `docs/QUESTIONS_FOR_OWNER.md` for morning review.

## Status board

| # | Milestone | Status | Acceptance evidence |
|---|---|---|---|
| M0 | Repo audit & plan | ✅ done (auto-approved per §19.1) | `docs/MIGRATION.md`, this file, `ARCHITECTURE.md`, ADR-0001…0015, `docs/data/DATASET_AUDIT.md` |
| M1 | Foundation | ✅ done (clean-clone check re-run at M12) | CI backend/frontend green; core stack healthy (~445 MiB measured) |
| M2 | Historical ingestion | ✅ done | primary 5_3_2016 + secondary final-2017; deterministic Parquet; idempotent S3 load; Airflow 3.3.2 DAG |
| M3 | dbt + analytics | ✅ done | 94 dbt tests incl. SQL oracle for history windows; FINDINGS.md (44 numbers independently re-derived) |
| M4 | ML | ✅ done | champion release_risk_ci/3 (XGBoost): test PR-AUC 0.811 / ROC 0.881 vs base 0.279; calibration gate passed; MODEL_CARD/RESULTS generated |
| M5 | Live risk | ⏳ GitHub Action ✅ (57 tests); risk endpoint + GitHub App next | — |
| M6 | Deployment events | ✅ done | 379 unit + 87 integration at merge; 12 review defects fixed |
| M7 | Telemetry + demo | ✅ ingest/consumer/rollups/compose demo profile; ⏳ live demo run | e2e test: payment = probable origin (HIGH) through the full pipeline |
| M8 | Regression engine | ✅ done | control false alarms 2/200; job handlers wired |
| M9 | Graph + triage | ✅ done | payment HIGH 30/30, fraud HIGH 30/30 seeds |
| M10 | SaaS layer | ✅ done | 374 tests incl. 231-case cross-tenant/RBAC matrix; ADR-0019..0021; RLS deferred to M12 (Q13) |
| M11 | Frontend | ⏳ M11a shell/marketing ✅; wiring to real APIs next | 272 vitest tests |
| M12 | Hardening & launch | ⬜ | — |

Legend: ⬜ not started · ⏳ in progress · ✅ done (acceptance demonstrated by commands in `PROGRESS.md`) · ⛔ blocked (see `BLOCKERS.md`)

## Environment constraints (measured 2026-10-13)

| Constraint | Value | Consequence for the plan |
|---|---|---|
| Host | Apple M4, 10 cores, 16 GB RAM, macOS | — |
| Host free disk | **≈ 7.2 GiB (97% full)** | One shared Python image for api/worker/mlflow/dbt/demo services. Small images (`postgres:16-alpine`, `apache/kafka-native`). Airflow in a thin extra venv layer. A disk guard in the Makefile refuses heavy targets below 2 GiB free. |
| Container engine | Colima: **2 CPU / 4 GiB RAM** / 20 GiB VM disk. Also runs 4 unrelated containers (~150 MB). | Profiles are started selectively. Every service has a `mem_limit`. Kafka uses the native image. The DAG is verified with Airflow's task-level runner (`dag.test()`) when the full scheduler doesn't fit. Resizing Colima is an owner action (`QUESTIONS_FOR_OWNER.md`). |
| Old prototype containers | `releaselens-backend` :8000, `releaselens-postgres` :5432 (compose project `releaselens`) | New compose project name **`rlens`**. Host ports avoid 5432/8000 (ADR-0015). The old containers are left untouched. |
| Docker CLI default context | `desktop-linux` (daemon not running) | Makefile exports `DOCKER_CONTEXT=colima` unless overridden. |

## Milestone scope

Each milestone ends with: tests + lint + typecheck green for the touched components → acceptance
checks run → `PROGRESS.md` updated with exact commands and results → conventional commit(s) →
self-review against spec §2, §8.3, §12, §16.

### M1 — Foundation
- uv workspace (`packages/rl_core`, `services/api`, `services/worker`, `ml`, `data/analysis`,
  `demo/services/*`). One lockfile. ruff + mypy configured.
- `services/api`: FastAPI app factory, settings (pydantic-settings), `/healthz` (liveness) and
  `/readyz` (Postgres + Kafka checks), structured JSON logging with `trace_id`/`span_id`
  (OTel-aware) and request IDs, async SQLAlchemy engine, Alembic baseline migration
  (organizations, users, memberships, api_keys skeleton + `tenant_id` conventions).
- `services/worker`: process skeleton with graceful shutdown and a health endpoint.
- `apps/web`: Next.js (pinned per ADR-0014) + Tailwind + shadcn baseline, health page that
  calls the API.
- `infra/compose`: one `compose.yaml` with profiles `core`, `data`, `demo`, healthchecks,
  `mem_limit`s, `.env.example`. `infra/kafka/bootstrap.sh`, `infra/otel/collector.yaml`.
- Makefile targets: `up-core`, `up-data`, `up-demo`, `down`, `test`, `lint`, `typecheck`,
  `seed`, `demo`, `doctor` (checks docker context, disk, ports).
- `.github/workflows/backend.yml`, `frontend.yml`, `data.yml`, `security.yml` (e2e later).
- **Acceptance:** `make up-core` from a clean clone → all core services healthy; `make test lint`
  green; Alembic `upgrade head` + `downgrade base` round-trip.

### M2 — Historical ingestion
- Data source per **ADR-0004**.
- `data/analysis/profile_dataset.py` (replaces the empty profiler) → `docs/data/DATA_DICTIONARY.md`
  generated from the real schema with null rates and `pre_build`/`post_build` tags.
- Airflow DAG `historical_pipeline` tasks `download_or_locate_dataset → validate_raw_schema →
  convert_to_parquet → load_raw_s3`, idempotent by dataset version (content hash).
- **Acceptance:** DAG runs twice → identical object listing and checksums; real row/project counts
  in `PROGRESS.md`.

### M3 — dbt + analytics
- dbt-duckdb project: staging → intermediate → marts → `ml/mart_build_inputs` with generic +
  custom tests (finished-before rule, label bounds, no duplicate builds).
- `data/analysis` statistical report → `docs/analytics/FINDINGS.md` + JSON. Every finding has n,
  effect size, test, CI, and a causation caveat.
- `export_analytics_to_postgres` (idempotent swap-load).

### M4 — ML
- `rl_core.features` (schema version, forbidden list, history windows), dbt Python model for
  `mart_build_features` (ADR-0006), splits (chronological, cross-project, cold-start), 5 models,
  calibration gate (ADR-0012), SHAP, MLflow logging + registry aliases, model card,
  `docs/ml/RESULTS.md`.

### M5 — Live risk
- `POST /api/v1/risk/assessments`, `ci_runs` history table, online features, parity test,
  GitHub App webhook handling (signature, dedupe, Check Runs) behind an interface with
  fixture-based tests, composite GitHub Action (`integrations/github-action`).

### M6 — Deployment events
- Envelope v1/v2, producer, idempotent consumer, retry/backoff/pause-resume, DLQ, outbox relay,
  `analysis_jobs` scheduling. testcontainers suite for every failure scenario in spec §10.

### M7 — Telemetry + demo
- OTLP ingest (protobuf + JSON), span consumer → partitioned `spans`, `service_edges`,
  `service_minute_rollups`, retention job. Five demo services, payment v1/v2 env switch, k6 scripts,
  `demo/scenarios/run_demo.sh`.

### M8 — Regression engine / M9 — Graph + triage
- `rl_core.regression`, `selftime`, `graph`, `triage`, `blast_radius` with Hypothesis property
  tests. Demo control experiment (v1→v1 no alarm), payment scenario (origin = payment, HIGH),
  fraud scenario (origin = fraud).

### M10 — SaaS layer / M11 — Frontend / M12 — Hardening
- As spec §13, §14, §16, §17. Stripe and GitHub run in test/fixture mode until owner credentials
  exist (`BLOCKERS.md`).

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Disk exhaustion during image builds or data conversion | High | High | `make doctor` disk guard; shared image; stream conversions; no duplicate data copies |
| 4 GiB Colima can't host core + data + demo together | High | Medium | Profiles started selectively, `mem_limit`s, task-level DAG runner; owner asked to resize |
| On-disk dataset lacks spec's `gh_*` change features | Certain | High | ADR-0004 |
| Model quality on pre-build features is modest | Medium | Medium | Report honestly against baselines; never inflate; cold-start + cross-project numbers shown |
| External accounts (GitHub App, Stripe, Slack) unavailable overnight | Certain | Low | Interfaces + signature-verified fixtures; `BLOCKERS.md` |
