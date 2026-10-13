# Progress log

> **Restart protocol (spec §19.2):** read the master prompt, then this file, `PLAN.md`,
> `DECISIONS.md`, `BLOCKERS.md`, `git log --oneline -20`; run `make check` to confirm the real
> state; continue from **Next step**. Tests beat this file if they disagree.

## Current state (2026-10-13 09:55 IST)

- **Done and pushed:**
  - M0 and M1.
  - M2: both datasets, primary 5_3_2016.
  - M3a: dbt project and feature mart, run `m3-run1`.
  - M6: deployment events.
  - M8/M9 core logic.
  - M5 GitHub Action.
  - M11a: web design system, shell and marketing site.
  - Contracts: FEATURE_DEFINITIONS, RISK_ASSESSMENTS, ANALYSIS_RESULTS.
- **Real feature mart:**
  - Location: `s3://releaselens-data/features/release_features/schema=1.0.0/run_id=m3-run1/part-00000.parquet`, 128,617 rows, sha256 `384f66b6…`.
  - Splits: train 90,031 / validation 19,293 / test 19,293.
  - Cut points: validation from 2015-01-10T08:05:34Z, test from 2015-04-17T18:41:22Z.
- **In flight.** A 06:30 IST session-limit stop interrupted these agents, and they were then resumed:
  1. M7 + M8 wiring, worktree branch `worktree-agent-a5ea34aa43cf5ac41`, migration `0003_telemetry`.
  2. M10 SaaS layer, worktree branch `worktree-agent-a37fc8e4e8d135faf`, migration `0003_saas`.
     **Renumber to `0004_saas` (down_revision `0003_telemetry`) when merging.**
  3. M4 ML, worktree branch `worktree-agent-ac96dff39debc0085`.
  4. Main tree workflow `wg1l9c7xw`: dbt adversarial review plus the bandit B608 fixes in
     `data/pipeline`, and M3b analytics (`data/analysis`, no migration yet).
- **Recovery if a session dies:** each worktree branch holds committed work. For each one, run
  `git log main..<branch>`, then run its tests inside the worktree, then
  `git merge <branch>` into main and resolve migration numbering. The analytics Postgres export
  becomes migration `0005_analytics` after M7 and M10 merge.
- **Security CI:** bandit B608 ×12 in `data/pipeline` (being fixed by the dbt reviewer). Pushes go
  through `make push` (gitleaks-gated, with the `.gitleaks.toml` allowlist).
- **Disk:** host ≈ 2.0 GiB free, Colima VM ≈ 2 GiB free.
- **Next steps after the merges:**
  - Compose services: mlflow, demo profile (collector, 5 services, k6), mailpit.
  - `demo/scenarios/run_demo.sh` end to end.
  - M5 risk endpoint serving the champion model.
  - M11 wiring of the web pages to the real APIs.
  - M12 benchmarks, security scans, README and DONE.md.

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
