#!/usr/bin/env bash
# ReleaseLens end-to-end demo (spec §11.1). Everything below is real: real services, real spans,
# real Kafka/Postgres, real regression analysis. Nothing is hard-coded except the *injected*
# regression knobs (that is the point of a demo).
#
#   demo/scenarios/run_demo.sh                       # SCENARIO=payment (payment v1 -> v2 regresses)
#   SCENARIO=fraud   demo/scenarios/run_demo.sh      # fraud's own code path slows; payment unchanged
#   SCENARIO=control demo/scenarios/run_demo.sh      # payment v1 -> v1 redeploy: must NOT alarm
#
# Tunables (env): BASELINE_MINUTES (default 11), POST_MINUTES (default 10), RATE (req/s, 20),
# KEEP_STACK=1 to leave the demo services running afterwards, DEMO_MEMBER_EMAIL=you@example.com
# to make that (already signed-up) user an owner of the demo organization, so the dashboard
# shows the run when they sign in.
# The "demo" environment uses shorter analysis windows (baseline 10 min, warm-up 1 min, post 8 min;
# configured in the worker, recorded in every result) so a run takes ~25 minutes, not ~50.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(pwd)
SCENARIO=${SCENARIO:-payment}
BASELINE_MINUTES=${BASELINE_MINUTES:-11}
POST_MINUTES=${POST_MINUTES:-10}
RATE=${RATE:-20}
API=${RL_API_URL:-http://localhost:28000}
OUT_DIR="$ROOT/demo/scenarios/out"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$SCENARIO"
export DOCKER_CONTEXT=${DOCKER_CONTEXT:-colima}
COMPOSE=(docker compose -f infra/compose/compose.yaml --env-file infra/compose/.env)

log() { printf '\n\033[1m[%s] %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '\n\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 is required"; }
need docker; need curl; need python3

case "$SCENARIO" in
  payment) DEPLOY_SERVICE=payment; NEW_VERSION=v2; PREV_VERSION=v1
           export RL_DEMO_PAYMENT_VERSION=v2 RL_DEMO_PAYMENT_EXTRA_LATENCY_MS=250 RL_DEMO_PAYMENT_ERROR_RATE=0.035 ;;
  fraud)   DEPLOY_SERVICE=fraud; NEW_VERSION=v2; PREV_VERSION=v1
           export RL_DEMO_FRAUD_VERSION=v2 RL_DEMO_FRAUD_EXTRA_LATENCY_MS=250 ;;
  control) DEPLOY_SERVICE=payment; NEW_VERSION=v1; PREV_VERSION=v1 ;;
  *) die "unknown SCENARIO=$SCENARIO (payment|fraud|control)" ;;
esac
mkdir -p "$OUT_DIR"

# ---------------------------------------------------------------- 1. core stack + demo tenant
log "1/7 core stack"
[ -f infra/compose/.env ] || cp infra/compose/.env.example infra/compose/.env
"${COMPOSE[@]}" --profile core up -d --wait --wait-timeout 300 >/dev/null

SLUG=${DEMO_ORG_SLUG:-demo-$(date -u +%m%d%H%M)}
log "2/7 demo organization '$SLUG' + API key"
ORG_ID=$("${COMPOSE[@]}" run --rm -T api python -m rl_api.admin create-org --name "Demo data ($SCENARIO)" --slug "$SLUG" 2>/dev/null | tail -1)
KEY=$("${COMPOSE[@]}" run --rm -T api python -m rl_api.admin create-key --org "$SLUG" \
      --scopes deployments:write,telemetry:write,risk:write,read --name "demo-$RUN_ID" 2>/dev/null | tail -1)
[[ "$KEY" == rlk_* ]] || die "could not create an API key"
echo "org $ORG_ID · key ${KEY:0:16}… (printed once, not stored)"
if [ -n "${DEMO_MEMBER_EMAIL:-}" ]; then
  "${COMPOSE[@]}" run --rm -T api python -m rl_api.admin add-member --org "$SLUG" \
      --email "$DEMO_MEMBER_EMAIL" --role owner \
    || die "could not add $DEMO_MEMBER_EMAIL to '$SLUG' (sign up in the web app first)"
fi
export RL_DEMO_INGEST_KEY="$KEY"

# ---------------------------------------------------------------- 3. demo services, baseline
log "3/7 demo services (all v1) + collector"
( unset RL_DEMO_PAYMENT_VERSION RL_DEMO_PAYMENT_EXTRA_LATENCY_MS RL_DEMO_PAYMENT_ERROR_RATE \
        RL_DEMO_FRAUD_VERSION RL_DEMO_FRAUD_EXTRA_LATENCY_MS RL_DEMO_FRAUD_ERROR_RATE
  "${COMPOSE[@]}" --profile demo up -d --wait --wait-timeout 300 \
    otel-collector gateway orders inventory payment fraud >/dev/null )

TOTAL_MINUTES=$((BASELINE_MINUTES + POST_MINUTES))
log "4/7 k6: ${RATE} req/s for ${TOTAL_MINUTES} min (baseline ${BASELINE_MINUTES} min)"
"${COMPOSE[@]}" --profile demo run --rm -d --name "rlens-k6-$RUN_ID" k6 run /scripts/checkout.js \
  -e RATE="$RATE" -e DURATION="${TOTAL_MINUTES}m" -e TARGET=http://gateway:8000 >/dev/null
sleep $((BASELINE_MINUTES * 60))

# ---------------------------------------------------------------- 5. pre-deploy risk assessment
log "5/7 pre-deployment risk assessment (change facts extracted from this repo's HEAD by the GitHub Action extractor)"
FACTS=$(python3 integrations/github-action/extract_change_facts.py --repo . --head HEAD --previous HEAD~1 --language java 2>/dev/null || echo '{}')
RISK=$(curl -s -o "$OUT_DIR/$RUN_ID.risk.json" -w '%{http_code}' -X POST "$API/api/v1/risk/assessments" \
  -H "x-releaselens-key: $KEY" -H 'content-type: application/json' \
  --data "$(python3 - "$FACTS" "$DEPLOY_SERVICE" <<'PY'
import json, sys, datetime
facts = json.loads(sys.argv[1] or "{}")
body = facts if "change_facts" in facts else {"change_facts": {}}
body.setdefault("repository", "demo/" + sys.argv[2])
body.setdefault("commit_sha", "0" * 40)
body.setdefault("event", "push")
body["triggered_at"] = datetime.datetime.now(datetime.UTC).isoformat()
print(json.dumps(body))
PY
)" || true)
case "$RISK" in
  200|201) python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(f\"risk {d['risk_score']}/100 band={d['risk_band']} model={d['model_version']}\")" "$OUT_DIR/$RUN_ID.risk.json" ;;
  *) echo "risk assessment unavailable (HTTP $RISK) — continuing; see docs/api/RISK_ASSESSMENTS.md" ;;
esac

# ---------------------------------------------------------------- 6. deploy + deployment event
log "6/7 deploy $DEPLOY_SERVICE:$NEW_VERSION (container swap) + deployment event"
"${COMPOSE[@]}" --profile demo up -d --no-deps --force-recreate --wait "$DEPLOY_SERVICE" >/dev/null
DEPLOYED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEP=$(curl -sf -X POST "$API/api/v1/deployments" -H "x-releaselens-key: $KEY" -H 'content-type: application/json' \
  --data "{\"service\":\"$DEPLOY_SERVICE\",\"environment\":\"demo\",\"version\":\"$NEW_VERSION\",\"previous_version\":\"$PREV_VERSION\",\"commit_sha\":\"$(git rev-parse HEAD)\",\"deployed_at\":\"$DEPLOYED_AT\",\"deployer\":\"run_demo.sh\"}") \
  || die "deployment event rejected"
EVENT_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["event_id"])' <<<"$DEP")
echo "event $EVENT_ID at $DEPLOYED_AT"

# ---------------------------------------------------------------- 7. wait for the final analysis
log "7/7 waiting for the final analysis (post window ends ~8 min after deploy in env 'demo')"
DEPLOYMENT_ID=""
for _ in $(seq 1 120); do
  DEPLOYMENT_ID=$(curl -sf "$API/api/v1/deployments?service=$DEPLOY_SERVICE&limit=1" -H "x-releaselens-key: $KEY" \
    | python3 -c 'import json,sys; items=json.load(sys.stdin).get("items",[]); print(items[0]["id"] if items else "")' || true)
  [ -n "$DEPLOYMENT_ID" ] && break; sleep 5
done
[ -n "$DEPLOYMENT_ID" ] || die "deployment was never persisted by the worker"

for _ in $(seq 1 240); do
  if curl -sf "$API/api/v1/deployments/$DEPLOYMENT_ID/analysis" -H "x-releaselens-key: $KEY" -o "$OUT_DIR/$RUN_ID.analysis.json" \
     && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if any(a.get("kind")=="final" for a in d.get("analyses",[])) else 1)' "$OUT_DIR/$RUN_ID.analysis.json"; then
    break
  fi
  sleep 15
done

python3 - "$OUT_DIR/$RUN_ID.analysis.json" "$SCENARIO" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
final = next(a for a in d["analyses"] if a["kind"] == "final")
print(f"status={final.get('status')} severity={final.get('severity_score')} ({final.get('severity_band')})")
print(f"probable origin={final.get('probable_origin')} confidence={final.get('confidence')}")
for line in (final.get("result", {}).get("origin", {}) or {}).get("reasoning", [])[:6]:
    print("  ·", line)
PY
echo
echo "deployment: http://localhost:23000/app/deployments/$DEPLOYMENT_ID"
echo "raw result: $OUT_DIR/$RUN_ID.analysis.json"

if [ "${KEEP_STACK:-0}" != "1" ]; then
  "${COMPOSE[@]}" --profile demo stop otel-collector gateway orders inventory payment fraud >/dev/null
fi
