# Shared Python image for every Python process in the stack (api, worker, migrate, and later
# mlflow, pipeline tasks and the demo services). One image keeps the disk footprint small:
# all containers share the same layers (docs/PLAN.md § Environment constraints).
#
# Build context: repository root.   docker build -f infra/docker/python.Dockerfile .

ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "uv==0.12.12"

WORKDIR /app

# 1) Dependency layer: only lockfile + member manifests, so code edits don't bust the cache.
COPY pyproject.toml uv.lock ./
COPY packages/rl_core/pyproject.toml packages/rl_core/pyproject.toml
COPY packages/rl_db/pyproject.toml packages/rl_db/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
COPY data/pipeline/pyproject.toml data/pipeline/pyproject.toml
COPY demo/services/pyproject.toml demo/services/pyproject.toml
COPY data/analysis/pyproject.toml data/analysis/pyproject.toml
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace && rm -rf /root/.cache

# DuckDB's httpfs extension (s3:// reads/writes), fetched once per dependency set rather than on
# every code change, and copied into the runtime image so containers never download extension
# code at run time. DuckDB looks in $HOME/.duckdb/extensions; the app user's HOME is /home/app.
RUN mkdir -p /opt/duckdb-home && HOME=/opt/duckdb-home /app/.venv/bin/python -c \
    "import duckdb; duckdb.sql('INSTALL httpfs'); print(duckdb.sql(\"SELECT extension_name, installed FROM duckdb_extensions() WHERE extension_name = 'httpfs'\").fetchall())"

# 2) Workspace members (non-editable, so the runtime image needs no source tree).
COPY packages packages
COPY services services
COPY data/pipeline data/pipeline
COPY demo/services demo/services
COPY data/analysis data/analysis
RUN uv sync --frozen --no-dev --all-packages --no-editable && rm -rf /root/.cache

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app

COPY --from=builder --chown=app:app /opt/duckdb-home /home/app
COPY --from=builder --chown=app:app /app/.venv /app/.venv

WORKDIR /app
USER app

# Each compose service supplies its own command (python -m rl_api, python -m rl_worker, …).
CMD ["python", "-c", "print('specify a command, e.g. python -m rl_api')"]
