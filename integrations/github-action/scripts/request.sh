#!/usr/bin/env bash
# ReleaseLens action, step 3: POST the extracted facts to the API.
#
# * The key is written to a 0600 header file that curl reads (-H @file), so it never appears in
#   a command line, the process list or the log. The file is removed on exit.
# * Retries with exponential backoff on network errors, HTTP 429 and 5xx, inside a total time
#   budget (RL_TIMEOUT seconds). A 503 carrying the API's JSON error envelope means "no model
#   loaded" and is not retried. Redirects are not followed (the key stays with api-url).
# * Never fails the step: render_result.py decides, from the files written here.
#
# Env: RL_STATE (state dir), RL_API_URL, RL_API_KEY, RL_TIMEOUT, RL_VERSION,
#      RL_RETRY_BASE_DELAY (seconds, default 2), RL_MAX_ATTEMPTS (default 4).
set -uo pipefail

state="${RL_STATE:?RL_STATE is required}"
if [ "$(cat "$state/stage" 2>/dev/null)" != ready ] || [ ! -s "$state/request.json" ]; then
  exit 0
fi

req="$state/request.json"
resp="$state/response.json"
hdr="$state/auth.header"
base="${RL_API_URL:-https://api.releaselens.dev}"
url="${base%/}/api/v1/risk/assessments"
budget="${RL_TIMEOUT:-60}"
delay="${RL_RETRY_BASE_DELAY:-2}"
max_attempts="${RL_MAX_ATTEMPTS:-4}"

trap 'rm -f "$hdr"' EXIT
(
  umask 077
  printf 'Authorization: Bearer %s\n' "$(printf '%s' "${RL_API_KEY:-}" | tr -d '\r\n')" >"$hdr"
)

echo "ReleaseLens: POST ${url}"
deadline=$(($(date +%s) + budget))
attempt=0
code=000
while :; do
  attempt=$((attempt + 1))
  remaining=$((deadline - $(date +%s)))
  if [ "$remaining" -lt 1 ]; then remaining=1; fi
  connect=$((remaining < 10 ? remaining : 10))
  rm -f "$resp"
  code="$(curl --silent --show-error --proto '=http,https' \
    --max-time "$remaining" --connect-timeout "$connect" \
    --request POST \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json' \
    --header "User-Agent: releaselens-action/${RL_VERSION:-0.1.0}" \
    --header "@${hdr}" \
    --data-binary "@${req}" \
    --output "$resp" --write-out '%{http_code}' \
    "$url" 2>"$state/curl.err")" || code=000
  case "$code" in
    2??) break ;;
    503)
      # The API's own 503 (no champion model) carries {"error": ...}; retrying will not help.
      if grep -q '"error"' "$resp" 2>/dev/null; then break; fi
      ;;
    429 | 5?? | 000) ;;
    *) break ;;
  esac
  if [ "$attempt" -ge "$max_attempts" ]; then break; fi
  if [ $(($(date +%s) + delay)) -ge "$deadline" ]; then break; fi
  echo "ReleaseLens: attempt ${attempt} failed (HTTP ${code}); retrying in ${delay}s"
  sleep "$delay"
  delay=$((delay * 2))
done

printf '%s' "$code" >"$state/http_status"
if [ "$code" = 000 ]; then
  printf 'network error after %s attempt(s): %s' "$attempt" "$(tail -n 1 "$state/curl.err" 2>/dev/null)" >"$state/message"
fi
printf requested >"$state/stage"
echo "ReleaseLens: HTTP ${code} after ${attempt} attempt(s)"
exit 0
