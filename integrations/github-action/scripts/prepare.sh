#!/usr/bin/env bash
# ReleaseLens action, step 1: validate inputs, mask the key, check history depth, resolve the
# build context (head, previous built commit, PR number, branch) and find Python 3.9+.
#
# Inputs arrive only through environment variables (never ${{ }} inside the script), so event
# fields such as branch names cannot inject shell code.
#
# Everything later steps need goes to $GITHUB_OUTPUT and to files in a private state directory:
#   stage   ready | skipped | extract_failed
#   message why a stage stopped early
set -euo pipefail

state_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/releaselens.XXXXXX")"
out() { printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"; }
stage() { # the first reason to stop wins
  if [ "$(cat "$state_dir/stage")" != ready ]; then return 0; fi
  printf '%s' "$1" >"$state_dir/stage"
  printf '%s' "${2:-}" >"$state_dir/message"
}
one_line() { printf '%s' "$1" | tr '\r\n' '  '; }
out state-dir "$state_dir"
printf ready >"$state_dir/stage"

# ------------------------------------------------------------------ the key: mask, never print
key="$(printf '%s' "${RL_API_KEY:-}" | tr -d '\r\n')"
if [ -n "$key" ]; then
  echo "::add-mask::${key}"
fi

# ------------------------------------------------------------------ inputs
fail_on="$(one_line "${RL_FAIL_ON_BAND:-none}" | tr '[:lower:]' '[:upper:]')"
case "$fail_on" in
  "" | NONE) fail_on=none ;;
  HIGH | CRITICAL) ;;
  *)
    echo "::error title=ReleaseLens::fail-on-band must be none, HIGH or CRITICAL (got '$(one_line "${RL_FAIL_ON_BAND:-}")')"
    exit 1
    ;;
esac
out fail-on-band "$fail_on"

timeout="$(one_line "${RL_TIMEOUT:-60}")"
if ! [[ "$timeout" =~ ^[0-9]+$ ]] || [ "$timeout" -lt 1 ]; then
  echo "::error title=ReleaseLens::timeout-seconds must be a positive integer (got '${timeout}')"
  exit 1
fi
out timeout "$timeout"

api_url="$(one_line "${RL_API_URL:-}")"
api_url="${api_url:-https://api.releaselens.dev}"
if ! [[ "$api_url" =~ ^https?://[^[:space:]]+$ ]]; then
  echo "::error title=ReleaseLens::api-url must start with https:// or http:// (got '${api_url}')"
  exit 1
fi
if [[ "$api_url" =~ ^http:// ]] && ! [[ "$api_url" =~ ^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/|$) ]]; then
  echo "::warning title=ReleaseLens::api-url uses plain http to a non-local host; the API key would travel unencrypted. Use https."
fi
out api-url "${api_url%/}"

if [ -z "$key" ]; then
  stage skipped "api-key is empty, so no score was requested (GitHub does not pass secrets to workflows triggered from forks)"
fi

# ------------------------------------------------------------------ python
py=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    py="$(command -v "$candidate")"
    break
  fi
done
out python "$py"
if [ -z "$py" ]; then
  stage extract_failed "Python 3.9 or newer (python3) is required on the runner"
fi

# ------------------------------------------------------------------ git checkout and depth
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  stage extract_failed "the working directory is not a git checkout; run actions/checkout first"
  exit 0
fi
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "::warning title=ReleaseLens::Shallow clone detected. Set fetch-depth: 0 on actions/checkout. Facts that need history (built commits, file hotness, team size, repository age) are sent as unknown (null), never as guesses."
fi

# ------------------------------------------------------------------ build context
is_sha() { [[ "$1" =~ ^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$ ]]; }
event=push
head="${GH_SHA:-}"
prev=""
pr=""
branch="${GH_REF_NAME:-}"
case "${GH_EVENT_NAME:-}" in
  pull_request | pull_request_target)
    event=pull_request
    head="${GH_PR_HEAD_SHA:-${GH_SHA:-}}"
    prev="${GH_PR_BASE_SHA:-}"
    pr="${GH_PR_NUMBER:-}"
    branch="${GH_HEAD_REF:-${GH_REF_NAME:-}}"
    ;;
  push)
    prev="${GH_BEFORE:-}"
    ;;
  *)
    echo "::notice title=ReleaseLens::Event '$(one_line "${GH_EVENT_NAME:-unknown}")' is scored like a push of the checked-out commit with no previous build (only the head commit counts)."
    ;;
esac
commit_sha="$head"
if [ -n "${RL_PREVIOUS_SHA:-}" ]; then
  prev="$(one_line "$RL_PREVIOUS_SHA")"
fi
if [ -n "$prev" ] && ! is_sha "$prev"; then
  echo "::warning title=ReleaseLens::Ignoring previous commit '$(one_line "$prev")': not a full commit SHA."
  prev=""
fi
if [[ "$prev" =~ ^0+$ ]]; then
  prev="" # new branch: there is no previous build
fi
if [ -n "$pr" ] && ! [[ "$pr" =~ ^[0-9]+$ ]]; then
  pr=""
fi

if ! is_sha "$head"; then
  head="$(git rev-parse HEAD)"
  commit_sha="$head"
fi
if ! git cat-file -e "${head}^{commit}" 2>/dev/null; then
  local_head="$(git rev-parse HEAD)"
  # actions/checkout's default for pull requests is the test merge commit refs/pull/N/merge,
  # whose second parent is the PR head. Its first-parent diff is exactly the PR's change.
  if [ "$event" = pull_request ] && git cat-file -p "$local_head" | grep -qx "parent ${head}"; then
    echo "::notice title=ReleaseLens::PR head ${head} is not in the clone; using the checked-out test merge commit. Use fetch-depth: 0 for exact facts."
    head="$local_head"
  else
    stage extract_failed "commit ${head} is not in the checkout; check it out with actions/checkout (fetch-depth: 0)"
  fi
fi

out event "$event"
out head "$head"
out commit-sha "$commit_sha"
out previous "$prev"
out pull-request "$pr"
out branch "$(one_line "$branch")"
