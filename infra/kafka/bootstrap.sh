#!/usr/bin/env bash
# Create/verify ReleaseLens Kafka topics (ADR-0007). Idempotent.
# Topic names, partitions and retention live in ONE place: services/worker/src/rl_worker/kafka_admin.py
#
# Usage (host):  KAFKA_BOOTSTRAP=localhost:29092 infra/kafka/bootstrap.sh
# In compose the `kafka-init` service runs the same module inside the rlens/python image.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec uv run python -m rl_worker.kafka_admin bootstrap --bootstrap-servers "${KAFKA_BOOTSTRAP:-localhost:29092}"
