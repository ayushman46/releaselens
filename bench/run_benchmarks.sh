#!/usr/bin/env bash
# ReleaseLens API benchmarks (spec §17 M12, docs/benchmarks/README.md).
#
#   bench/run_benchmarks.sh                 # all three: risk, deployments, otlp
#   BENCHES="risk" DURATION=120s bench/run_benchmarks.sh
#
# Needs the core stack (make up-core) and, for the risk benchmark, a loaded model
# (minio + mlflow from the data profile). k6 runs in a container on the compose network and
# talks to the API container directly, so host networking is not part of the measurement.
# Each benchmark gets its own throwaway organization and API key, so per-key rate limits
# (docs/security/AUTH.md) apply exactly as they would to a customer.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
export DOCKER_CONTEXT=${DOCKER_CONTEXT:-colima}
COMPOSE=(docker compose -f infra/compose/compose.yaml --env-file infra/compose/.env)
NETWORK=${RL_COMPOSE_NETWORK:-rlens_default}
K6_IMAGE=grafana/k6:2.3.0
BENCHES=${BENCHES:-risk deployments otlp}
DURATION=${DURATION:-60s}
OUT_DIR="$ROOT/bench/out"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
"${COMPOSE[@]}" --profile core ps --status running --services | grep -qx api || die "the api service is not running (make up-core)"

for bench in $BENCHES; do
  [ -f "bench/k6/$bench.js" ] || die "unknown benchmark $bench"
  slug="bench-$bench-$(date -u +%m%d%H%M%S)"
  "${COMPOSE[@]}" run --rm -T api python -m rl_api.admin create-org --name "Benchmark ($bench)" --slug "$slug" >/dev/null 2>&1
  key=$("${COMPOSE[@]}" run --rm -T api python -m rl_api.admin create-key --org "$slug" \
        --scopes deployments:write,telemetry:write,risk:write,read --name "k6-$RUN_ID" 2>/dev/null | tail -1)
  [[ "$key" == rlk_* ]] || die "could not create an API key"
  echo "== $bench ($DURATION, org $slug)"
  docker run --rm --network "$NETWORK" -v "$ROOT/bench/k6:/bench:ro" -v "$OUT_DIR:/out" \
    -e RL_KEY="$key" -e DURATION="$DURATION" ${RATE:+-e RATE="$RATE"} ${SPANS:+-e SPANS="$SPANS"} \
    -e OUT="/out/$RUN_ID-$bench.json" "$K6_IMAGE" run --quiet "/bench/$bench.js" \
    | tee "$OUT_DIR/$RUN_ID-$bench.txt" || echo "(k6 exited non-zero: a threshold failed; see the summary)"
done
echo "summaries: $OUT_DIR/$RUN_ID-*.json"
