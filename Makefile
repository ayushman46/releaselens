# ReleaseLens — single entry point for local development.  `make help` lists targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help

# Container engine: Colima on the dev machine (Docker Desktop's daemon is not running there).
# Override with `make DOCKER_CONTEXT=default …` or by exporting DOCKER_HOST yourself.
DOCKER_CONTEXT ?= $(shell docker context ls --format '{{.Name}}' 2>/dev/null | grep -qx colima && echo colima || echo default)
export DOCKER_CONTEXT
DOCKER_HOST_SOCK := $(shell docker context inspect $(DOCKER_CONTEXT) --format '{{.Endpoints.docker.Host}}' 2>/dev/null)

COMPOSE_DIR  := infra/compose
COMPOSE      := docker compose -f $(COMPOSE_DIR)/compose.yaml --env-file $(COMPOSE_DIR)/.env
MIN_FREE_GB  ?= 2
UV           ?= uv

# testcontainers talks to the Docker API directly and ignores `docker context`.
TC_ENV := DOCKER_HOST=$(DOCKER_HOST_SOCK) TESTCONTAINERS_RYUK_DISABLED=true

# Every profile, so ps/logs/down also see the data stack and the optional full Airflow deployment.
ALL_PROFILES := --profile core --profile data --profile airflow-full --profile demo
# Data-profile containers that `make stop-data` stops (core services are left running).
DATA_SERVICES := minio airflow-api-server airflow-scheduler airflow-dag-processor airflow-triggerer
# Extra arguments for `airflow dags test`, e.g. make pipeline CONF='{"skip_training": true}'
CONF ?=
# Dataset key for `make pipeline` (rl_pipeline.datasets; empty = the DAG default, the primary
# travistorrent-5-3-2016): make pipeline DATASET=travistorrent-final-2017-01-25
DATASET ?=
# --conf for `airflow dags test`: DATASET alone, CONF alone, or both merged (DATASET wins only if
# CONF has no "dataset"; a conflicting pair is an error). Recursive (=) so it is only evaluated
# by the pipeline recipe.
PIPELINE_CONF = $(shell DATASET='$(DATASET)' CONF='$(CONF)' python3 -c 'import json, os, sys; \
	c = json.loads(os.environ["CONF"] or "{}"); d = os.environ["DATASET"]; \
	(d and c.get("dataset", d) != d) and sys.exit("DATASET and CONF disagree on the dataset"); \
	c.update({"dataset": d} if d else {}); print(json.dumps(c) if c else "")')

PY_SRC := packages/rl_core/src packages/rl_db/src services/api/src services/worker/src \
	data/pipeline/src data/analysis/src demo/services/src integrations/github-action

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ environment
$(COMPOSE_DIR)/.env:
	cp $(COMPOSE_DIR)/.env.example $@

.PHONY: doctor
doctor: ## Check docker context, free disk and required tools
	@echo "docker context: $(DOCKER_CONTEXT) ($(DOCKER_HOST_SOCK))"
	@docker info --format 'engine {{.ServerVersion}} · {{.NCPU}} CPU · {{.MemTotal}} bytes RAM' \
		|| { echo "✗ docker engine unreachable (try: colima start)"; exit 1; }
	@free_kb=$$(df -k . | awk 'NR==2 {print $$4}'); free_gb=$$((free_kb / 1048576)); \
		if [ $$free_gb -lt $(MIN_FREE_GB) ]; then echo "✗ only $${free_gb} GiB free (< $(MIN_FREE_GB))"; exit 1; \
		else echo "✓ $${free_gb} GiB free disk"; fi
	@for t in uv pnpm node git; do command -v $$t >/dev/null && echo "✓ $$t" || echo "✗ $$t missing"; done

# ------------------------------------------------------------------ python
.PHONY: install
install: ## Install the Python workspace (uv) and web deps (pnpm)
	$(UV) sync --all-packages
	cd apps/web && pnpm install --frozen-lockfile

.PHONY: lint
lint: ## Ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Auto-format Python code
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

.PHONY: typecheck
typecheck: ## mypy (strict for rl_core / rl_db)
	$(UV) run mypy $(PY_SRC)

.PHONY: test
test: ## Unit tests (no Docker needed; the Airflow DAG tests run via pipeline-test-dag)
	$(UV) run pytest -m "not integration" packages services data demo integrations

.PHONY: test-integration
test-integration: doctor ## Integration tests (testcontainers: Postgres, Kafka, S3)
	$(TC_ENV) $(UV) run pytest -m integration packages services data demo

.PHONY: check
check: lint typecheck test ## Lint + typecheck + unit tests

# ------------------------------------------------------------------ web
.PHONY: web-check
web-check: ## Web lint + typecheck + tests
	cd apps/web && pnpm lint && pnpm typecheck && pnpm test

# ------------------------------------------------------------------ stack
.PHONY: build
build: $(COMPOSE_DIR)/.env doctor ## Build local images
	$(COMPOSE) --profile core build

.PHONY: up-core
up-core: $(COMPOSE_DIR)/.env doctor ## Start the core stack (postgres, kafka, api, worker, web) and wait until healthy
	$(COMPOSE) --profile core up -d --build --wait --wait-timeout 300
	@$(MAKE) --no-print-directory ps

.PHONY: ps
ps: ## Show stack status
	$(COMPOSE) $(ALL_PROFILES) ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'

.PHONY: logs
logs: ## Tail logs (SERVICE=api to filter)
	$(COMPOSE) $(ALL_PROFILES) logs -f --tail=100 $(SERVICE)

.PHONY: down
down: ## Stop the stack (keeps volumes)
	$(COMPOSE) $(ALL_PROFILES) down

.PHONY: clean-stack
clean-stack: ## Stop the stack AND delete its volumes (rlens project only)
	$(COMPOSE) $(ALL_PROFILES) down -v

.PHONY: smoke
smoke: ## Probe the running core stack's health endpoints
	@curl -fsS http://localhost:$${RL_API_HOST_PORT:-28000}/healthz && echo
	@curl -fsS http://localhost:$${RL_API_HOST_PORT:-28000}/readyz && echo
	@curl -fsS http://localhost:$${RL_WORKER_HOST_PORT:-28001}/readyz && echo
	@curl -fsS http://localhost:$${RL_WEB_HOST_PORT:-23000}/api/health && echo

# ------------------------------------------------------------------ data (M2, ADR-0013)
.PHONY: build-data
build-data: $(COMPOSE_DIR)/.env doctor ## Build rlens/python, then rlens/airflow (FROM rlens/python, so in order)
	$(COMPOSE) --profile data build minio-init
	$(COMPOSE) --profile data build airflow-init

.PHONY: up-data
up-data: build-data ## Start postgres + object store (minio/silo) and create the buckets
	$(COMPOSE) --profile data up -d --wait --wait-timeout 180 postgres minio
	$(COMPOSE) --profile data run --rm minio-init

.PHONY: pipeline
pipeline: up-data ## Run historical_pipeline once via `airflow dags test` (DATASET=<key>, CONF='{...}')
	@conf='$(PIPELINE_CONF)'; \
		if [ -z "$$conf" ] && [ -n "$(if $(DATASET)$(CONF),x)" ]; then \
			echo "invalid DATASET/CONF (see the error above)" >&2; exit 2; fi; \
		echo "historical_pipeline conf: $${conf:-<defaults>}"; \
		$(COMPOSE) --profile data run --rm airflow-cli \
			airflow dags test historical_pipeline $${conf:+--conf "$$conf"}

# ------------------------------------------------------------------ dbt (M3a, ADR-0006)
# Run id of a dbt build: outputs land in marts/<model>/run_id=<id>/ and
# features/release_features/schema=<v>/run_id=<id>/ (re-using an id overwrites with equal bytes).
DBT_RUN_ID ?= local
# --no-sync: dbt runs in the current workspace venv (`make install` syncs it).
DBT_PARAMS = {"run_id": "$(DBT_RUN_ID)"}

.PHONY: dbt-build
dbt-build: doctor ## dbt deps + build staging/intermediate/marts/ml on the host against the object store (DBT_RUN_ID=<id>)
	@curl -fsS -o /dev/null "http://localhost:$${RL_MINIO_HOST_PORT:-29000}/minio/health/live" \
		|| { echo "✗ object store not reachable on :$${RL_MINIO_HOST_PORT:-29000} (make up-data)"; exit 1; }
	$(UV) run --no-sync python -m rl_pipeline dbt_build_all --params-json '$(DBT_PARAMS)'

.PHONY: dbt-fixture
dbt-fixture: ## dbt build on the SYNTHETIC fixture: local Parquet, no S3 (what CI runs)
	$(UV) run --no-sync python -m rl_pipeline dbt_build_fixture

# ------------------------------------------------------------------ analytics (M3b, spec §7.5)
# Run id of the dbt build whose marts/features the report reads (s3://releaselens-data/...).
ANALYTICS_RUN_ID ?= m3-run1
ANALYTICS_DUCKDB ?= data/lake/dbt/releaselens.duckdb

.PHONY: analytics-report
analytics-report: ## M3b report: docs/analytics/{findings.json,FINDINGS.md} + S3 reports/analytics/ (ANALYTICS_RUN_ID=<id>)
	@curl -fsS -o /dev/null "http://localhost:$${RL_MINIO_HOST_PORT:-29000}/minio/health/live" \
		|| { echo "✗ object store not reachable on :$${RL_MINIO_HOST_PORT:-29000} (make up-data)"; exit 1; }
	$(UV) run --no-sync python -m rl_analysis report --run-id $(ANALYTICS_RUN_ID) \
		$(if $(wildcard $(ANALYTICS_DUCKDB)),--duckdb-path $(ANALYTICS_DUCKDB))

.PHONY: test-analysis
test-analysis: ## Analytics unit tests (SYNTHETIC marts fixture; no S3 needed)
	$(UV) run pytest -m "not integration" data/analysis

.PHONY: pipeline-test-dag
pipeline-test-dag: $(COMPOSE_DIR)/.env doctor ## DAG integrity checks (mypy + pytest) inside the Airflow image; no DB/S3 needed
	$(COMPOSE) --profile data build minio-init
	docker build -f infra/docker/airflow.Dockerfile --target dag-test -t rlens/airflow:dag-test .
	docker run --rm rlens/airflow:dag-test

.PHONY: up-airflow
up-airflow: up-data ## Full Airflow 3 deployment (UI :28080). Needs >= 8 GiB for Docker (ADR-0013/0015)
	$(COMPOSE) --profile data --profile airflow-full up -d --wait --wait-timeout 300 \
		airflow-api-server airflow-scheduler airflow-dag-processor airflow-triggerer
	@echo "Airflow UI: http://localhost:$${RL_AIRFLOW_HOST_PORT:-28080}"

.PHONY: stop-data
stop-data: $(COMPOSE_DIR)/.env ## Stop the data-profile containers (object store, Airflow); core services keep running
	$(COMPOSE) --profile data --profile airflow-full stop $(DATA_SERVICES)

# ------------------------------------------------------------------ security
.PHONY: secrets-scan
secrets-scan: ## Scan the main branch history for secrets (gitleaks; fails on any finding)
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:v8.30.1 git /repo --redact --log-opts="main" -v

.PHONY: push
push: secrets-scan ## Push main to origin ONLY if the secrets scan passes (the repo is public)
	git push origin main