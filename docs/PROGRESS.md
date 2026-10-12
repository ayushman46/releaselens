# Progress log

> **Restart protocol (spec §19.2):** read the master prompt, then this file, `PLAN.md`,
> `DECISIONS.md`, `BLOCKERS.md`, `git log --oneline -20`; run `make check` to confirm the real
> state; continue from **Next step**. Tests beat this file if they disagree.

## Current state

- **Current milestone:** M1 → closing (clean-clone `make up-core` check pending); M2, M6 and M8/M9
  core logic are being built in parallel (independent code paths).
- **Last completed step:** web skeleton + CI pushed (`3408e75`). GitHub Actions: backend ✅
  (lint, mypy, unit, testcontainers integration, image build), frontend ✅, security ✅
  (gitleaks, Trivy fs, pip-audit, bandit, pnpm audit), data ❌ only because `data/` had no tests
  yet (pytest exit 4, "collected 0 items").
- **Uncommitted, green locally:** `rl_core.stats` (Wilson, z-test, bootstrap, Mann–Whitney/Cliff's
  delta, chi-square/Cramér's V) + `rl_core.features` (schema, finished-before history,
  change/time features, offline Polars batch, online per-request) → 49 tests incl. Hypothesis
  oracle + offline/online parity. Waiting for the M2 lockfile changes to commit together.
- **In flight (background workflows):** M2 pipeline + Airflow orchestration (`data/pipeline`,
  `data/airflow`, `infra/docker/airflow.Dockerfile`); M6 deployment events (API keys, envelope,
  consumer, DLQ, jobs, outbox); M8/M9 rl_core regression engine/self-time/graph/triage.
  If a restart finds these half-done: run their tests, keep what is green, finish the rest.
- **Next step:** integrate + commit each workflow's output (tests green first), then M3 (dbt +
  analytics) → M4 (ML) → M5 → M7 → M10 → M11 → M12.
- **Environment notes:** Docker engine = Colima (`DOCKER_CONTEXT=colima`, 2 CPU / 4 GiB). Host disk
  ≈ 9–10 GiB free. The old prototype containers (`releaselens-backend` :8000,
  `releaselens-postgres` :5432) are still running and must not be touched; our stack is compose
  project `rlens` on ports 25432/29092/28000/28001/23000.

## Verification commands (current)

```bash
make check                 # ruff + mypy + unit tests
make test-integration      # testcontainers: Alembic round-trip on postgres:16-alpine
make up-core && make smoke # full core stack, health endpoints
```

## Log

### 2026-10-13 — M0 Repo audit & plan ✅ (auto-approved per spec §19.1)

Done:
- Audited the repo, the untracked raw data, the surviving Python prototype (copied read-only out
  of the `releaselens-backend` image) and traces of the Go/ClickHouse prototype (only Docker
  volumes and Go service images remain, no source). See `docs/MIGRATION.md`.
- Dataset audit (4 parallel investigators + critic): the on-disk archive is the raw
  travistorrent-tools harvester output (no `gh_*` change features). Real counts: **64,736 labelled
  builds** (19 projects, 2011-10-12 → 2016-08-31 UTC); Java subset (12 projects) **39,728 builds**;
  failed+errored share excluding canceled 30.20% (all) / 28.77% (Java-12).
  Details: `docs/data/DATASET_AUDIT.md`.
- Lossless disk recovery: the in-repo partial copy (82,713 files) was byte-identical to the full
  extracted archive (rsync checksum diff = 0 files). Each file was replaced by an APFS
  copy-on-write clone of its twin, with a SHA-256 check before and after every replacement:
  free disk went from 7,580,876 KB to 9,832,040 KB. No data was removed.
- Docs: `MIGRATION.md`, `PLAN.md`, `ARCHITECTURE.md`, `DECISIONS.md` (ADR-0001…0015),
  `dev/CONVENTIONS.md`.

### 2026-10-13 — M1 Foundation (in progress)

Verified (commands run, results):
- `uv sync --python 3.12` → workspace resolves (fastapi 0.143.0, sqlalchemy 2.1.4, pydantic 2.14.1,
  alembic 1.20.0, asyncpg 0.32.0, confluent-kafka 2.16.0, opentelemetry-sdk 1.45.1, pytest 9.1.1,
  ruff 0.17.0, mypy 2.4.0, testcontainers 4.15.0).
- `uv run pytest -m "not integration" packages services` → **19 passed**.
- `DOCKER_HOST=… uv run pytest -m integration packages/rl_db` → **1 passed** (upgrade head →
  autogenerate diff == [] → downgrade base → upgrade/downgrade again) on `postgres:16-alpine`.
- `uv run ruff check .` / `ruff format --check .` → clean; `uv run mypy <src>` → no issues.
- `docker compose … --profile core up -d --wait postgres kafka kafka-init migrate api worker` →
  all healthy; `kafka-init` created 6 topics (deployment.events 6p, telemetry.spans 12p,
  analysis.results 6p, 3 DLQs 1p); `migrate` exited 0.
- `curl localhost:28000/readyz` → `{"status":"ready","checks":{"postgres":{"status":"ok"},"kafka":{"status":"ok"}}}`;
  `curl localhost:28001/readyz` → ready.

Known issues:
- Starlette 1.7 warns that `TestClient` with `httpx` is deprecated (`httpx2`); harmless for now.
