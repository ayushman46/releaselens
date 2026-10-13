# Progress log

> **Restart protocol (spec §19.2):** read the master prompt, then this file, `PLAN.md`,
> `DECISIONS.md`, `BLOCKERS.md`, `git log --oneline -20`; run `make check` to confirm the real
> state; continue from **Next step**. Tests beat this file if they disagree.

## Current state (2026-10-13 ~09:10 UTC, M12 in progress)

- **main = `5cc6bc2`** (pushed). M0–M11 done; M5 and M11 completed after the parallel merge.
- **Verified on main at `5cc6bc2`:**
  - ruff, format and mypy clean.
  - API suite: 497 passed (unit + integration).
  - Worker suite: 119 passed. One SIGTERM offset assertion was racy under full-suite load; it
    was fixed to check its own message's partition, then passed 3 of 3 runs.
  - rl_db suite: 78 passed, including the 5 new RLS tests.
  - 544 non-integration API, worker and core tests pass.
- **Live demo, payment scenario (06:06 UTC):** REGRESSED HIGH, probable origin payment (HIGH);
  see `docs/demo/DEMO_SCRIPT.md`.
- **M12 done so far:**
  - MLflow OOM fix: one worker, no job runner, 768 MiB.
  - Model-loader retries are bounded and can no longer die.
  - Webhook-delivery purge in housekeeping.
  - **Postgres RLS (ADR-0023, Q13 resolved).**
  - Analysis jobs get their own DB timeout, and job timeouts are labelled correctly (seen
    live).
  - k6 scripts in `bench/`.
- **Discarded run:** the control run at 08:12 UTC was discarded, because I ran testcontainers
  suites in the same 2-CPU VM during its post window. Every service slowed at once, giving
  REGRESSED with no single origin (LOW confidence). It must be re-run with the VM otherwise
  idle.
- **Next steps:**
  1. Rebuild `rlens/python:dev` at HEAD.
  2. Restart the core stack (migration 0007 applies).
  3. Re-run the control, then fraud, live scenarios with nothing else in the VM.
  4. Run the k6 benchmarks (`bench/run_benchmarks.sh`) and write `docs/benchmarks/README.md`.
  5. Accessibility check of the web app, final README, `DONE.md`.
- **Disk:** the VM hit 100% during a rebuild (09:00). It was recovered by removing only my own
  build-cache records (`docker buildx prune -a --filter id=…`) and our unused
  `rlens/airflow:dev` image (rebuildable). Host ≈ 5.8 GiB free.

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
