# Engineering conventions & cross-component contracts

This file is the contract between components. Change it only together with every consumer.

## 1. Python workspace (uv)

| Member path | Distribution name | Import package | Purpose |
|---|---|---|---|
| `packages/rl_core` | `rl-core` | `rl_core` | Pure domain logic: models, event schemas, features, regression engine, graph, triage. No DB/Kafka/HTTP I/O. |
| `packages/rl_db` | `rl-db` | `rl_db` | SQLAlchemy 2 (async) models, engine/session factory, tenant-scoped repositories, **Alembic migrations** (`packages/rl_db/alembic`). |
| `services/api` | `rl-api` | `rl_api` | FastAPI application. |
| `services/worker` | `rl-worker` | `rl_worker` | Kafka consumers, job runner, outbox relay. |
| `ml` | `rl-ml` | `rl_ml` | Training / evaluation / registry. |
| `data/pipeline` | `rl-pipeline` | `rl_pipeline` | Historical pipeline task callables (Airflow-agnostic; Airflow DAGs only wire them). |
| `data/analysis` | `rl-analysis` | `rl_analysis` | Statistical analysis + report generation. |
| `demo/services` | `rl-demo` | `rl_demo` | The five instrumented demo services (`rl_demo.gateway`, `.orders`, `.inventory`, `.payment`, `.fraud`). |

- Layout: `src/<import_package>/` inside each member, tests in `<member>/tests/`.
- Python `>=3.12,<3.13`. Build backend `uv_build`.
- One lockfile `uv.lock` at the root. Add deps with `uv add --package <dist-name> <dep>`.
- Lint/format: `ruff` (config in root `pyproject.toml`). Types: `mypy` (strict for `rl_core`, `rl_db`).
- Tests: `pytest`, `asyncio_mode = auto`. Markers: `integration` (needs Docker/testcontainers), `slow`.
  `make test` runs unit tests; `make test-integration` runs integration tests.

## 2. Environment variables (all services read `RL_*` via pydantic-settings)

| Variable | Example (compose network) | Used by |
|---|---|---|
| `RL_ENV` | `dev` \| `test` \| `prod` | all |
| `RL_LOG_LEVEL` | `INFO` | all |
| `RL_LOG_FORMAT` | `json` \| `console` | all |
| `RL_DATABASE_URL` | `postgresql+asyncpg://rlens:rlens@postgres:5432/rlens` | api, worker, migrations |
| `RL_KAFKA_BOOTSTRAP` | `kafka:9092` | api, worker |
| `RL_CORS_ORIGINS` | `http://localhost:23000` | api |
| `RL_S3_ENDPOINT` | `http://minio:9000` | pipeline, ml, mlflow |
| `RL_S3_ACCESS_KEY` / `RL_S3_SECRET_KEY` / `RL_S3_REGION` | `rlens` / `rlens-dev-secret` / `us-east-1` | pipeline, ml |
| `RL_S3_BUCKET_DATA` | `releaselens-data` | pipeline, ml |
| `RL_S3_BUCKET_MLFLOW` | `releaselens-mlflow` | mlflow |
| `RL_MLFLOW_TRACKING_URI` | `http://mlflow:5000` | ml, api |
| `RL_SMTP_HOST` / `RL_SMTP_PORT` | `mailpit` / `1025` | worker, api |
| `RL_SMTP_USERNAME` / `RL_SMTP_PASSWORD` / `RL_SMTP_STARTTLS` / `RL_MAIL_FROM` | unset / unset / `false` / `ReleaseLens <no-reply@releaselens.local>` | worker, api |
| `RL_SECRET_KEY` (+ `RL_SECRET_KEY_PREVIOUS`, comma-separated) | ≥ 32 random chars; **required when `RL_ENV=prod`** (dev/test fall back to a public dev key) | api, worker |
| `RL_WEB_BASE_URL` / `RL_API_BASE_URL` | `http://localhost:23000` / `http://localhost:28000` | api (links, redirects), worker (alert links) |
| `RL_TRUSTED_PROXIES` | CIDRs whose `X-Forwarded-For` is believed, e.g. `10.0.0.0/8` | api |
| `RL_COOKIE_DOMAIN`, `RL_ACCESS_TOKEN_TTL_S`, `RL_REFRESH_TOKEN_TTL_S`, `RL_SESSION_MAX_AGE_S` | unset, `900`, `1209600`, `2592000` | api |
| `RL_ARGON2_TIME_COST` / `_MEMORY_KIB` / `_PARALLELISM`, `RL_PASSWORD_HASH_CONCURRENCY` | `3` / `65536` / `4`, `2` | api |
| `RL_RATE_LIMIT_ENABLED`, `RL_RATE_LIMIT_{AUTH,LOGIN_ACCOUNT,INGEST}_{BURST,PER_MINUTE}` | `true`; see docs/security/AUTH.md §6 | api |
| `RL_MAX_BODY_BYTES` | `1048576` | api |
| `RL_GITHUB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI` | unset (→ 503) | api |
| `RL_STRIPE_SECRET_KEY` / `RL_STRIPE_WEBHOOK_SECRET` / `RL_STRIPE_PRICE_TEAM` | unset (→ 503); test-mode keys only | api |
| `RL_NOTIFIER_ENABLED`, `RL_NOTIFIER_GROUP_ID`, `RL_NOTIFICATION_*`, `RL_SLACK_TIMEOUT_S`, `RL_MAINTENANCE_ENABLED`, `RL_MAINTENANCE_INTERVAL_S` | `true`, `rl-notifier`, see `rl_worker.settings`, `5`, `true`, `3600` | worker |
| `RL_DATASETS_DIR` | `/datasets` (one sub-directory per registered dataset, `rl_pipeline.datasets`) | pipeline |
| `RL_LAKE_DIR` | `/lake` (Parquet staging, DuckDB spill, dbt work dir `<lake>/dbt`) | pipeline |
| `RL_DBT_PROJECT_DIR` | `/opt/airflow/dbt` (default `<repo>/data/dbt`; read-only, see docs/data/PIPELINE.md §8) | pipeline |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | standard OTel | all (self-telemetry, demo) |

Secrets never have defaults in code outside `RL_ENV=dev|test`. `.env.example` is committed,
and `.env` is git-ignored. Empty values count as unset (`env_ignore_empty`), so compose can pass
optional secrets through as `${VAR:-}`.

## 3. Compose (`infra/compose/compose.yaml`, project name `rlens`)

Host ports deliberately avoid 5432/8000/18000/18080 (already in use on the dev machine) and
are all overridable in `.env`.

| Service | Profile | Container port | Host port | Image |
|---|---|---|---|---|
| `postgres` | core | 5432 | 25432 | `postgres:16-alpine` (pinned per ADR-0014) |
| `kafka` | core | 9092 (internal) / 29092 (host listener) | 29092 | `apache/kafka-native` (ADR-0007) |
| `kafka-init` | core | — | — | `apache/kafka` CLI image or same as kafka (runs `infra/kafka/bootstrap.sh`) |
| `migrate` | core | — | — | `rlens/python` (runs `alembic upgrade head`, exits) |
| `api` | core | 8000 | 28000 | `rlens/python` |
| `worker` | core | 8001 (health) | 28001 | `rlens/python` |
| `web` | core | 3000 | 23000 | `rlens/web` |
| `mailpit` | core | 1025 / 8025 | 21025 / 28025 (127.0.0.1 only) | `axllent/mailpit:v1.31.3` |
| `minio` + `minio-init` | data | 9000 / 9001 | 29000 / 29001 | per ADR-0009 |
| `mlflow` | data | 5000 | 25000 | `rlens/python` (`mlflow server`) |
| `airflow` | data | 8080 | 28080 | `rlens/airflow` (ADR-0013) |
| `otel-collector` | demo | 4317 / 4318 | 24317 / 24318 | `otel/opentelemetry-collector-contrib` |
| `gateway`, `orders`, `inventory`, `payment`, `fraud` | demo | 8000 | gateway only: 28100 | `rlens/python` |
| `k6` | demo (run-once) | — | — | `grafana/k6` |

Every long-running service declares a `healthcheck` and a `mem_limit`.

## 4. HTTP conventions

- Liveness `GET /healthz` → `200 {"status":"ok"}` (no dependencies checked).
- Readiness `GET /readyz` → `200` only if dependencies (Postgres, Kafka) respond within 2 s;
  otherwise `503` with per-dependency detail.
- JSON error shape: `{"error": {"code": "<snake_case>", "message": "...", "request_id": "..."}}`.
- Every response carries `x-request-id`. Logs are one JSON object per line, with `ts`, `level`,
  `logger`, `msg`, `request_id`, `trace_id`, `span_id`, `tenant_id` (when known), and `service`.

## 5. Identifiers & time

- Primary keys: UUIDv7 (`rl_core.ids.uuid7()`), stored as Postgres `uuid`.
- All timestamps are timezone-aware UTC (`timestamptz`). Span times are integer nanoseconds.
- `tenant_id` = `organizations.id` (UUID).

## 6. Git

- Conventional commits: `feat(m1): …`, `fix(api): …`, `docs: …`, `test(core): …`, `chore: …`.
- Never commit secrets, datasets, `.venv`, or build output. The GitHub repository
  (`ayushman46/releaselens`) is **public**: run `make secrets-scan` (gitleaks) before pushing.
