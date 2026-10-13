# Progress log

> **Restart protocol (spec §19.2):** read the master prompt, then this file, `PLAN.md`,
> `DECISIONS.md`, `BLOCKERS.md`, `git log --oneline -20`; run `make check` to confirm the real
> state; continue from **Next step**. Tests beat this file if they disagree.

## Current state (2026-10-13, after integrating all parallel branches)

- **main = `290f861`**, which includes M0–M4, M6–M10, the M5 GitHub Action and M11a.
- **Verified on merged main:**
  - ruff and format clean; mypy clean on 199 files; bandit clean repo-wide.
  - 874 unit tests pass.
  - Integration suites pass per branch at merge time (446 for M7+M10).
- **Model:** champion `release_risk_ci/3`, XGBoost, registered in MLflow. MLflow ran as a host
  process during M4; the compose `mlflow` service exists.
  - Test PR-AUC 0.811, ROC-AUC 0.881, against a 0.279 base rate.
  - Most of the signal is persistence: after a passing build, PR-AUC is 0.320 against a 0.122
    base rate.
  - See `docs/ml/RESULTS.md` and `docs/ml/MODEL_CARD.md`.
- **Next steps:**
  1. M5: the risk endpoint serving the champion, `ci_runs` history, GitHub App webhooks and
     Check Runs (fixture-tested), and the offline/online parity test on historical builds.
  2. M11: wire the pages to the real APIs (auth, deployments, regressions, services, map,
     analytics findings.json, model).
  3. Rebuild images and run `demo/scenarios/run_demo.sh` for real (payment, fraud, control).
  4. M12: k6 benchmarks, Trivy image scans, RLS, README, DEMO_SCRIPT, DONE.md.
- **Disk:** host ≈ 4 GiB free. Only `rlens`-labelled images and our own build cache may be
  pruned.

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
