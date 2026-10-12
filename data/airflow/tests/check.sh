#!/bin/sh
# DAG checks run inside rlens/airflow:dag-test (`make pipeline-test-dag`):
#   1. mypy with Airflow's own types (Airflow venv) plus our first-party packages from the
#      workspace venv, with the same settings as the root pyproject.toml;
#   2. pytest DAG-integrity tests.
set -eu

SITE=/app/.venv/lib/python3.12/site-packages
STUBS=/tmp/mypy-path
mkdir -p "$STUBS"
for pkg in rl_pipeline rl_core; do
    if [ -d "$SITE/$pkg" ]; then ln -sfn "$SITE/$pkg" "$STUBS/$pkg"; fi
done

echo "== mypy"
MYPYPATH="$STUBS" python -m mypy \
    --python-version 3.12 --cache-dir /tmp/mypy-cache \
    --check-untyped-defs --no-implicit-optional --warn-redundant-casts --warn-unused-ignores \
    --ignore-missing-imports --explicit-package-bases --follow-imports silent \
    /opt/airflow/dags /opt/airflow/bin /opt/airflow/tests

echo "== pytest"
exec python -m pytest -q -p no:cacheprovider /opt/airflow/tests
