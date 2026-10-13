# Benchmarks

Every number on this page was measured on the hardware below, with the command next to it.
Raw k6 summaries are written to `bench/out/` (git-ignored) and can be regenerated with
`bench/run_benchmarks.sh`.

## Hardware and conditions

| Item | Value |
|---|---|
| Host | Apple M4, 10 cores, 16 GB RAM, macOS 26.3 |
| Container VM | Colima, **2 vCPU / 4 GiB RAM / 20 GiB disk**, shared by every container below |
| Stack | `make up-core` plus `minio` and `mlflow`: Postgres 16.15, Kafka 4.3.1 (KRaft, native image), API (1 uvicorn worker, 640 MiB limit), worker (1 GiB limit), web |
| Load generator | k6 2.3.0 in a container on the compose network (`rlens_default`), talking to `api:8000` directly; no host port forwarding in the path |
| Rate limits | left **on** at their defaults, so the rates below are the per-key limits a customer key gets (risk 600/min, deployment ingest 3,000/min; `docs/security/AUTH.md`). OTLP ingest had no per-key limit in the measured build; one (6,000/min) was added afterwards |
| Isolation | nothing else ran in the VM during a benchmark (no demo traffic, tests or builds) |

These are capacity numbers for a laptop VM, not a production sizing. The API runs a single
uvicorn worker. Scaling out is horizontal (stateless API, Postgres-backed rate limits).

## API latency (k6, 2026-10-13, commit `5cc6bc2`)

`bench/run_benchmarks.sh`, constant arrival rate, a fresh organization and key per benchmark.
Latency is k6's `http_req_duration`, measured inside the compose network.

| Endpoint | Load | Requests | Failed | p50 | p90 | p95 | p99 | max |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `POST /api/v1/risk/assessments` (feature build, XGBoost score, SHAP; new commit each time) | 8 req/s for 60 s | 481 | 0 | 22.2 ms | 31.7 ms | **40.0 ms** | 133.7 ms | 612 ms |
| `POST /api/v1/deployments` (validate, idempotency, outbox write) | 10 req/s for 60 s | 601 | 0 | 19.4 ms | 26.2 ms | **30.4 ms** | 43.4 ms | 169 ms |
| `POST /v1/traces` (OTLP JSON, 50 spans per request) | 20 req/s for 30 s (1,000 spans/s) | 601 | 0 | 16.7 ms | 28.1 ms | **49.8 ms** | 156.9 ms | 321 ms |
| `POST /v1/traces` (OTLP JSON, 100 spans per request) | 40 req/s for 20 s (4,000 spans/s) | 801 | 0 | 10.8 ms | 13.2 ms | **14.6 ms** | 19.9 ms | 53 ms |

Every k6 check passed (2xx/202 on every request).

- **Risk:** the benchmark organizations have no CI history, so these requests were scored by the
  cold-start model (`release_risk_ci_cold/1`, 26 features). The history path adds the
  bounded history query. Its latency was not separately benchmarked.
- **Ingest throughput:** spans were counted in Postgres every 5 s while k6 ran.
  - At 1,000 spans/s offered, all 30,050 spans were persisted within about 5 s of the last
    request.
  - At 4,000 spans/s offered, persistence ran at 3,580–4,720 spans/s per 5-s interval and all
    80,100 spans were stored within about 5 s of the last request.
  - The ceiling was not reached. The run was stopped there because the VM disk had less than
    1 GB free.

## Deployment-to-detection latency (live demo, 2026-10-13)

Analyses run at deploy +5 min (`early_5m`), +10 min and +15 min (`final`). In the `demo`
environment the post window runs from deploy +1 min (warm-up) to +8 min. Detection latency is therefore the
schedule plus compute time. Measured from `regression_analyses.created_at` against the
deployment time, with about 90–100k spans per analysis:

| Run | First verdict | Stored at | Compute after the scheduled time |
|---|---|---:|---:|
| fraud v1 → v2 (injected +250 ms) | early_5m: REGRESSED HIGH, origin fraud (HIGH) | +377 s | 77 s |
| control payment v1 → v1 | early_5m: HEALTHY | +309 s | 9 s |

So a regression of this size is flagged, with its probable origin, about **6.3 minutes** after
the deploy event. The 5-minute wait is a configurable product choice
(`analysis_schedule`), not a processing limit. The earlier payment run (06:17 UTC) is not
used here: its early analyses were delayed by worker out-of-memory restarts that were fixed
afterwards (`e2e2858`).

## Historical pipeline and training (measured earlier, M2–M4)

| Stage | Wall clock | Source |
|---|---:|---|
| Ingest DAG, primary dataset, cold (`make pipeline`: locate, validate, convert to Parquet, load to S3) | DAG run 23.0 s (`make` 31 s) | `docs/data/PIPELINE.md` §run log |
| Same, repeat (manifest and checksums match, nothing re-uploaded) | DAG run 5.0 s (`make` 14 s) | same |
| dbt build (15 models, 91 tests) on the mart | 9.15–9.90 s, peak RSS ≈ 1.0 GB | `docs/data/PIPELINE.md` §dbt |
| Model training and evaluation (`python -m rl_ml.train`: 360.5 s of candidate search) | 383.3 s total | `docs/ml/RESULTS.md` §12 |

The ingest and dbt runs were on the Colima VM. Training ran as a host process.
