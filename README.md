# ReleaseLens

**Release intelligence for engineering teams.** ReleaseLens answers two questions:

1. **Before deploy:** *Based on historical delivery data, how risky does this change look, and why?*
   A model trained on real historical CI builds scores **CI/build-failure risk** (0–100) and
   explains the score with SHAP.
2. **After deploy:** *Did this deployment introduce a regression, where did it probably
   originate, and which services were affected?* It compares OpenTelemetry traces before and after
   the release with statistical gating, builds the service graph from real traces, names the
   **probable origin**, and computes the **blast radius**.

Product loop: **PREDICT → DEPLOY → OBSERVE → DETECT → EXPLAIN → LEARN**.
ReleaseLens never deploys code. It plugs into your existing GitHub / CI/CD through a GitHub App
and Action, a deployment-event API, and an OTLP endpoint.

> **Status:** under active development, milestone by milestone. See [`docs/PLAN.md`](docs/PLAN.md)
> for the status board and [`docs/PROGRESS.md`](docs/PROGRESS.md) for verified results with
> the exact commands that were run. Every number in this README comes from a recorded run.
> Anything not yet measured is marked `TBD — measured in Mx`.

## Quick start (local, ₹0)

Requirements: Docker (Colima or Docker Desktop), `uv` ≥ 0.12, Node 22+ with `pnpm`, GNU Make.

```bash
make install        # Python workspace (uv) + web deps (pnpm)
make up-core        # postgres, kafka (KRaft), api, worker, web — waits until healthy
make smoke          # probe health endpoints
open http://localhost:23000/status
```

| Service | URL |
|---|---|
| Web app | http://localhost:23000 |
| API (OpenAPI docs) | http://localhost:28000/docs |
| Worker health | http://localhost:28001/readyz |

Host ports sit in a `2xxxx` block to avoid clashing with other local stacks
(see [`docs/dev/CONVENTIONS.md`](docs/dev/CONVENTIONS.md)).

## Architecture

```mermaid
flowchart LR
  GH["GitHub + CI"] -- "Action: change facts" --> API
  CD["CD pipeline"] -- "POST /api/v1/deployments" --> API
  SVC["Your services (OTel)"] --> COL["OTel Collector"] -- "OTLP /v1/traces" --> API
  API["API (FastAPI)"] --> K[["Kafka (KRaft)"]] --> W["Worker"]
  API --> PG[("Postgres 16")]
  W --> PG
  AF["Airflow + dbt-duckdb"] --> S3[("S3 (local: MinIO-compatible)")]
  AF --> ML["MLflow registry"] -- "champion model" --> API
  WEB["Web (Next.js)"] --> API
```

Full diagrams, component responsibilities and data flows are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Every major choice has an ADR in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

## Data and methodology (honest version)

- **Training data:** TravisTorrent build data re-published by its authors on Figshare
  (CC BY 4.0). We train on the `travistorrent_5_3_2016` release, **Java subset: 128,819 builds
  across 402 projects (2011-06 → 2015-08)**, measured after an md5-verified download. A later
  re-publication (`final-2017-01-25`, 200,561 Java builds) was profiled and rejected for training
  because its change-size columns are effectively empty: source churn is 0 on 99.08% of builds and
  test churn on 100%. See [`docs/data/DATASET_AUDIT.md`](docs/data/DATASET_AUDIT.md), ADR-0004 and
  ADR-0017.
- **What the model predicts:** the risk that a **CI build fails** (failed or errored), using only
  information available *before* the build starts. It does **not** predict production outages.
  The current build's CI result and duration are shown *next to* the score, never inside it.
- **Leakage controls:** a past build counts as history only if it **finished before** the current
  build started (finish time is conservatively upper-bounded). This is enforced by property tests
  against a naive oracle and by offline/online parity tests.
- **Model metrics:** `TBD — measured in M4` (chronological, cross-project and cold-start splits).
- **Regression detection:** a metric is flagged only if every gate passes (sample size, relative
  and absolute deltas, and a seeded bootstrap CI or a z-test). Severity is a documented
  deterministic score, **not a probability**. The tool reports a **probable** origin, never a
  "root cause".

## Repository layout

```
apps/web/              Next.js app (marketing + dashboard)
services/api/          FastAPI: REST API, auth, ingest, risk inference, webhooks
services/worker/       Kafka consumers, Postgres job runner, outbox relay
packages/rl_core/      Pure domain logic: features, stats, regression engine, graph, triage
packages/rl_db/        SQLAlchemy models, tenant-scoped repositories, Alembic migrations
data/pipeline/         Historical pipeline tasks (download → validate → Parquet → S3)
data/airflow/          Airflow DAGs wiring the pipeline tasks
demo/                  Five instrumented demo services, k6 traffic, scripted scenarios
infra/                 Compose stack, OTel Collector config, Kafka topic bootstrap
docs/                  Plan, progress, ADRs, architecture, data & ML docs
```

## Testing

```bash
make check             # ruff + mypy + unit tests
make test-integration  # testcontainers: Postgres 16, Kafka 4.3 (KRaft), S3
make web-check         # eslint + tsc + vitest
```

CI runs on every push: backend, frontend, data quality, and security (gitleaks, Trivy,
pip-audit, bandit, pnpm audit). Third-party actions are pinned by commit SHA.

## Benchmarks

`TBD — measured in M12` with k6: risk-inference p50/p95, deployment-event API p95, ingest
spans/sec, deployment-to-detection latency, historical pipeline runtime. The hardware and
conditions will be stated next to each number.

## Known limitations

- TravisTorrent is historical (2012–2016 Travis CI, open source). CI failure is **not** the same
  as a production regression, and transfer to a 2026 team's pipeline is evaluated (cross-project,
  cold-start) but not guaranteed.
- The demo topology has five services. Spans are stored in Postgres, which is adequate at demo
  scale (ADR-0008 sets the thresholds for revisiting that choice).
- The local S3 server is `pgsty/silo`, a MinIO-compatible fork, because upstream MinIO images
  are no longer distributed (ADR-0009).

## Attribution

Historical CI data: Moritz Beller, Georgios Gousios, Andy Zaidman, *TravisTorrent: Synthesizing
Travis CI and GitHub for Full-Stack Research on Continuous Integration*, MSR 2017,
DOI [10.1109/MSR.2017.24](https://doi.org/10.1109/MSR.2017.24). Data re-published under
CC BY 4.0, DOI [10.6084/m9.figshare.19314170](https://doi.org/10.6084/m9.figshare.19314170).
