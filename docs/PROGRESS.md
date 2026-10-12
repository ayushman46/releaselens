# Progress log

> **Restart protocol (spec §19.2):** read the master prompt, then this file, `PLAN.md`,
> `DECISIONS.md`, `BLOCKERS.md`, `git log --oneline -20`; run `make check` to confirm the real
> state; continue from **Next step**. Tests beat this file if they disagree.

## Current state

- **Milestones:** M0 ✅ · M1 ✅ · M2 ✅ (`final-2017`) / extending to the primary `5_3_2016`
  dataset (ADR-0017) · M3 ⏳ · M6/M8/M9-core ⏳ · M7 demo services ✅ (ingest pending) ·
  M11a ✅.
- **Last commits:** `c680c8c` (M2 + rl_core features/stats + demo), `ceb9a81` (ADR-0017),
  `168445c` (M11a web). GitHub CI on `c680c8c`: backend ✅, data ✅, frontend ✅,
  security ❌ = bandit B608 (f-string SQL) ×12 in `data/pipeline` (DuckDB queries built from
  internal schema constants). **Next fix:** use DuckDB parameter binding for values and an
  allowlist + justified `# nosec B608` for identifiers. Deferred until the M2b agent releases
  `data/pipeline`.
- **In flight (background workflows):**
  1. M2b + M3 (`wc25a2zj9`): 5_3_2016 dataset spec → dbt project → analytics + Postgres export
     → reviews.
  2. M6 + M8/M9 core (`waq3y476j`): API keys, deployment envelope/consumer/DLQ/jobs/outbox;
     rl_core regression engine, self-time, graph, triage, blast radius.

  Uncommitted files under `packages/rl_db`, `services/*`, `packages/rl_core/{events,telemetry,
  regression,…}` and `data/` belong to these. If a restart finds them half-done: run their
  tests, keep what is green, finish the rest.
- **Disk:** host ≈ 2.1 GiB free, Colima VM ≈ 4 GiB free (VM disk file is at its 20 GiB cap).
  Prune only `rlens`-labelled dangling images and our own build cache. See Q11.
- **Next steps after the workflows:** commit M6, M8/M9 and M3 → fix bandit → M7 OTLP ingest + span
  consumer + demo compose profile + scenario script → M4 ML → M5 risk API + GitHub Action → M10
  SaaS layer → M11 wiring → M12.

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
