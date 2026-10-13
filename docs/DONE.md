# Done: ReleaseLens M0–M12

**Written 2026-10-13** at the end of the autonomous build (spec §19). This page is the summary.
`PROGRESS.md` has the commands, `PLAN.md` the status board, `DECISIONS.md` the 23 ADRs.
**Every number below was measured.** Where something was not done or not measured, this page
says so.

## What exists

| Area | What was built | Evidence |
|---|---|---|
| Historical data (M2–M3) | Pipeline: download, validate, Parquet, S3 (Airflow 3.3.2 DAG, idempotent, checksummed). dbt-duckdb marts with 94 tests, including a SQL oracle for the history windows. Analytics findings, independently re-derived. | `docs/data/PIPELINE.md`, `docs/analytics/FINDINGS.md` |
| Risk model (M4) | XGBoost champion `release_risk_ci/3` in the MLflow registry, with SHAP explanations and a calibration gate. A cold-start model for repositories with no history. Leakage tests (finished-before rule, offline/online parity). | `docs/ml/RESULTS.md`, `docs/ml/MODEL_CARD.md` |
| Live risk (M5) | `POST /api/v1/risk/assessments` serving both models (checksummed cache, registry fallback, bounded retries). A GitHub Action change-fact extractor. A GitHub App with Check Runs and webhooks (fixture mode). CI-run history. | `docs/api/RISK_ASSESSMENTS.md`, `docs/integrations/` |
| Deployment events (M6) | Validated, idempotent `POST /api/v1/deployments` → Kafka → worker → Postgres. Analysis jobs at +5, +10 and +15 min. | `docs/events/` |
| Telemetry (M7) | OTLP/HTTP ingest (protobuf and JSON, gzip, size caps, per-key rate limit) → Kafka → day-partitioned spans, edges and minute rollups. A five-service instrumented demo system with k6 traffic. | `docs/events/TELEMETRY.md` |
| Regression engine (M8–M9) | Gated detection (sample size, relative and absolute deltas, bootstrap CI, z-test or Fisher). Self-time that excludes outbound waits. A service graph from real traces. **Probable origin** with confidence and reasons. Blast radius with UNKNOWN classes. Severity score (not a probability). | ADR-0018, `docs/events/ANALYSIS_RESULTS.md` |
| SaaS layer (M10) | Organisations, RBAC, e-mail+password auth with opaque sessions and CSRF, API keys, audit log, alert rules (Slack encrypted at rest, e-mail), Stripe billing (test mode), plan limits and retention, rate limits. | `docs/security/AUTH.md`, `docs/billing/BILLING.md` |
| Frontend (M11) | Next.js 16 marketing site and dashboard. Every page is backed by the real API: overview, deployments, deployment detail, regressions, risk, services, service map, analytics, model, settings, onboarding. | 353 vitest tests; `docs/web/ACCESSIBILITY.md` |
| Hardening (M12) | Postgres row-level security on all 21 tenant-owned tables (ADR-0023). Model-loader hardening. Analysis-job DB timeouts. OTLP per-key limit. Webhook-delivery purge. k6 benchmarks. Live control and fraud runs. axe accessibility pass. bandit in `make check`. | below |

## Measured results

**Model** (test split: the most recent 15%, evaluated once; base rate 0.279):

- PR-AUC **0.811**, ROC-AUC **0.881**, Brier 0.109.
- Calibration gate passed (ECE 0.0155), so a probability is shown.
- Held-out projects: PR-AUC 0.825.
- Honest caveat: after a *passing* build, PR-AUC is **0.320** against a 0.122 base rate. Most
  of the signal is failure persistence.
- Cold-start model: test PR-AUC 0.537. It failed the calibration gate, so it shows no
  probability.

**Live demo** (`demo/scenarios/run_demo.sh`, real services, spans, Kafka and Postgres):

| Scenario | Verdict | Probable origin | Notes |
|---|---|---|---|
| payment v1 → v2 (+250 ms, ~3.5% errors) | REGRESSED, severity 62.0 HIGH | payment, HIGH | Self-time p95 104 → 352 ms. Own error rate 0 → 4.5% (p = 1.8e-23). Blast radius: orders and gateway; fraud healthy. |
| fraud v1 → v2 (+250 ms) | REGRESSED, severity 46.7 HIGH, in all 3 looks | fraud, HIGH (score 10 vs 2) | Payment was slowed by its call to fraud but was not blamed. Blast radius: payment, orders, gateway. |
| control: payment v1 → v1 | HEALTHY, in all 3 looks | none | Every job succeeded on the first attempt. Verdicts were stored 8–16 s after their scheduled time. |

**Benchmarks** (Colima 2 vCPU / 4 GiB on an Apple M4; per-key limits on; `docs/benchmarks/`):

| Measurement | Result |
|---|---|
| Risk assessment, 8 req/s | p50 22 ms, p95 40 ms |
| Deployment event, 10 req/s | p50 19 ms, p95 30 ms |
| OTLP ingest, 4,000 spans/s | p95 14.6 ms; all 80,100 spans persisted at about 3.7–4.7k spans/s |
| Deploy to first REGRESSED verdict | about 6.3 min (5-min early look plus 77 s of compute) |
| Pipeline | ingest DAG 23 s cold; dbt ≈ 10 s; training 383 s |

**Tests and checks** (2026-10-13):

- ruff, format, mypy and bandit clean.
- **907 unit tests** pass.
- Integration: API 497, worker 119, rl_db 78 (testcontainers: Postgres 16, Kafka 4.3).
- Web: 353 vitest tests, tsc clean.
- Extractor: 57.
- axe-core: **0 violations** on 25 pages.
- CI (backend, frontend, data, security: gitleaks, Trivy fs, pip-audit, bandit, pnpm audit)
  is green on the final commits.

## Not done, or not measured

- **External services need owner credentials** (`BLOCKERS.md`):
  - GitHub App (B1), Stripe test keys (B2), Slack (B3), optional AWS S3 (B4), GitHub OAuth
    (B5).
  - All five are exercised with signed fixtures and mocks, and answer `503 …_not_configured`
    until configured.
- **Playwright E2E** is written but has never been run, because no browsers could be installed
  (disk). Accessibility was checked with automated axe rules only, with no manual
  screen-reader audit.
- **Trivy** scans the repository filesystem in CI. Container *image* scans were not run (the
  local VM has no room for the vulnerability DB).
- **The model is Java- and Travis-era-only.** Transfer to a 2026 GitHub Actions pipeline is
  unmeasured. Feature fidelity between the extractor and TravisTorrent's definitions is not yet
  measured on a real repository's history. Non-Java repositories get unknown size facts and a
  warning.
- **Scale:** spans are stored in Postgres, which is fine at demo scale. The OTLP ingest ceiling
  was not reached in the benchmark. ADR-0008 sets the thresholds for moving spans out of
  Postgres.
- **The first payment run's detection timings** are excluded: worker out-of-memory restarts
  delayed them, and the restarts were fixed afterwards.

## Decisions waiting for you

`docs/QUESTIONS_FOR_OWNER.md` lists 16 items. The ones that need action or a reply:

- **Q4:** the old prototype containers (`releaselens-backend` :8000, `releaselens-postgres`
  :5432) are untouched. Remove them when you're ready.
- **Q5 and Q15:** Colima is 2 CPU / 4 GiB / 20 GiB. A larger VM (`--cpu 6 --memory 10
  --disk 40`) lets every profile run at once. Resizing restarts your other containers.
- **Q7:** the repository is **public** and has been pushed after every change (gitleaks-gated),
  as you asked. Make it private if you prefer.
- **Q14:** an earlier unscoped `docker image prune` removed about 17 untagged layers from
  other projects. That was my mistake, and only `rlens`-scoped cleanup has been used since.
- **Q16:** a local test account was created for the accessibility check.

Fully resolved during the build: Q13, Postgres row-level security, now implemented (ADR-0023).
