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

PY_SRC := packages/rl_core/src packages/rl_db/src services/api/src services/worker/src

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
test: ## Unit tests (no Docker needed)
	$(UV) run pytest -m "not integration" packages services

.PHONY: test-integration
test-integration: doctor ## Integration tests (testcontainers: Postgres, Kafka)
	$(TC_ENV) $(UV) run pytest -m integration packages services

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
	$(COMPOSE) --profile core --profile data --profile demo ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'

.PHONY: logs
logs: ## Tail logs (SERVICE=api to filter)
	$(COMPOSE) --profile core --profile data --profile demo logs -f --tail=100 $(SERVICE)

.PHONY: down
down: ## Stop the stack (keeps volumes)
	$(COMPOSE) --profile core --profile data --profile demo down

.PHONY: clean-stack
clean-stack: ## Stop the stack AND delete its volumes (rlens project only)
	$(COMPOSE) --profile core --profile data --profile demo down -v

.PHONY: smoke
smoke: ## Probe the running core stack's health endpoints
	@curl -fsS http://localhost:$${RL_API_HOST_PORT:-28000}/healthz && echo
	@curl -fsS http://localhost:$${RL_API_HOST_PORT:-28000}/readyz && echo
	@curl -fsS http://localhost:$${RL_WORKER_HOST_PORT:-28001}/readyz && echo
	@curl -fsS http://localhost:$${RL_WEB_HOST_PORT:-23000}/api/health && echo

# ------------------------------------------------------------------ security
.PHONY: secrets-scan
secrets-scan: ## Scan the git history for secrets (gitleaks via docker)
	docker run --rm -v "$(CURDIR):/repo:ro" zricethezav/gitleaks:v8.30.1 git /repo --redact -v
