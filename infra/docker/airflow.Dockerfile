# Airflow image for the historical pipeline (ADR-0013).
#
#   * Airflow 3.3.2 lives in its OWN virtualenv (/opt/airflow-venv), installed with the official
#     constraints file, so its pins (fastapi<0.137, SQLAlchemy 2.0.x, …) never touch the
#     workspace venv (/app/.venv) that rl_pipeline runs in.
#   * The final image is FROM rlens/python (shared base layers + /app/.venv), and DAG tasks call
#     rl_pipeline through @task.external_python(python="/app/.venv/bin/python").
#   * The venv is built in a separate stage FROM the same python:3.12-slim image that
#     rlens/python's runtime stage uses, so /opt/airflow-venv/bin/python points at the same
#     /usr/local/bin/python3.12 interpreter, and code edits in the workspace never invalidate the
#     (slow) Airflow install layer.
#
# Build context: repository root, after rlens/python:dev has been built:
#   docker build -f infra/docker/airflow.Dockerfile -t rlens/airflow:dev .
# DAG integrity tests (adds pytest + mypy on top, never shipped):
#   docker build -f infra/docker/airflow.Dockerfile --target dag-test -t rlens/airflow:dag-test .
# The default (last) stage is `runtime`, so a plain build never ships the test tooling.

ARG PYTHON_IMAGE=python:3.12-slim
ARG BASE_IMAGE=rlens/python:dev

# ---------------------------------------------------------------- airflow venv
FROM ${PYTHON_IMAGE} AS airflow-venv

ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_MINOR=3.12
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# --without-pip + `pip --python`: the venv never carries its own copy of pip (~12 MB).
# psycopg2-binary = metadata-DB driver (postgresql+psycopg2://); asyncpg = the async engine Airflow
# derives from it. Both are pinned by the constraints file, so no Postgres provider is needed.
RUN python -m venv --without-pip /opt/airflow-venv \
 && pip --python /opt/airflow-venv/bin/python install \
        "apache-airflow==${AIRFLOW_VERSION}" psycopg2-binary asyncpg \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_MINOR}.txt" \
 && find /opt/airflow-venv -depth -type d -name tests -path '*/site-packages/*/tests' -exec rm -rf {} + \
 && /opt/airflow-venv/bin/python -c "import airflow, psycopg2, asyncpg; print('airflow', airflow.__version__)"

# ---------------------------------------------------------------- airflow (shared base)
FROM ${BASE_IMAGE} AS airflow

USER root

COPY --from=airflow-venv /opt/airflow-venv /opt/airflow-venv

ENV AIRFLOW_HOME=/opt/airflow \
    PATH=/opt/airflow-venv/bin:/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags \
    RL_LAKE_DIR=/lake \
    RL_DATASET_DIR=/datasets/travistorrent

# /lake (parquet staging) and /opt/airflow/logs are volume mount points: creating them owned by
# uid 10001 makes Docker initialise fresh named volumes with that owner.
RUN mkdir -p /opt/airflow/logs /opt/airflow/bin /lake /datasets/travistorrent \
 && chown -R app:app /opt/airflow /lake \
 && /opt/airflow-venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version" \
 && /app/.venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"

COPY --chown=root:root data/airflow/bin /opt/airflow/bin
COPY --chown=root:root data/airflow/dags /opt/airflow/dags

WORKDIR /opt/airflow
USER app

CMD ["airflow", "version"]

# ---------------------------------------------------------------- DAG integrity tests
FROM airflow AS dag-test

USER root
# Same pytest/mypy versions as uv.lock; installed only in this throwaway target. mypy runs here
# (not in the workspace venv) because only this venv has Airflow's type information.
RUN pip --python /opt/airflow-venv/bin/python install --no-cache-dir "pytest==9.1.1" "mypy==2.4.0"
COPY --chown=root:root data/airflow/tests /opt/airflow/tests
USER app

CMD ["sh", "/opt/airflow/tests/check.sh"]

# ---------------------------------------------------------------- runtime (default target)
FROM airflow AS runtime
