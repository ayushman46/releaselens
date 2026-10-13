# Risk assessments API (contract, M5)

`POST /api/v1/risk/assessments`: scope `risk:write` (header `Authorization: Bearer <key>` or
`x-releaselens-key: <key>`). The tenant comes **only** from the key. Rate limited per key
(`RL_RATE_LIMIT_RISK_BURST` = 60, `RL_RATE_LIMIT_RISK_PER_MINUTE` = 600; `429` + `Retry-After`).

> A CI-failure risk estimate from historical patterns, not a production outage probability.

## Request

```json
{
  "repository": "acme/payments",
  "commit_sha": "3f2a…40-hex",
  "pull_request": 123,
  "branch": "main",
  "event": "pull_request",
  "triggered_at": "2026-10-13T02:00:00Z",
  "ci_run_url": "https://github.com/acme/payments/actions/runs/1",
  "previous_built_sha": "9c1b…",
  "extractor": { "name": "releaselens-action", "version": "0.1.0" },
  "change_facts": {
    "src_churn": 120, "test_churn": 40,
    "files_added": 1, "files_deleted": 0, "files_modified": 6,
    "tests_added": 2, "tests_deleted": 0,
    "src_files": 5, "doc_files": 1, "other_files": 1,
    "sloc": 48211, "test_lines_per_kloc": 410.2, "test_cases_per_kloc": 21.5, "asserts_per_kloc": 55.0,
    "num_all_built_commits": 3, "team_size": 9, "by_core_team_member": true,
    "is_pr": true, "commits_on_files_touched": 14, "repo_age_days": 1203
  }
}
```

| Field | Rule |
|---|---|
| `repository` | `owner/repo` (`[A-Za-z0-9_.-]{1,100}` each side). Stored lower-cased. Required. |
| `commit_sha` | 40 hex (or 64 for SHA-256 repositories). Stored lower-cased. Required. |
| `event` | `push` \| `pull_request`. Required. |
| `triggered_at` | ISO 8601 **with** a timezone; at most 1 hour in the future. Required. It is the prediction moment: only CI runs that **finished before it** count as history. |
| `pull_request` | Integer ≥ 1 or `null`. |
| `branch`, `ci_run_url`, `previous_built_sha`, `extractor` | Optional, stored for display. `ci_run_url` must be `http(s)`. |
| `change_facts` | Keys ⊆ `rl_core.features.change.BUILD_INPUT_FIELDS` (definitions: [`docs/data/FEATURE_DEFINITIONS.md`](../data/FEATURE_DEFINITIONS.md)). Values: JSON numbers, booleans or `null`; strings and non-finite numbers are rejected. **Unknown keys → `422`.** A missing key or `null` means *unknown* (NaN), never 0. Negative counts are treated as unknown. |

Any other top-level key (including `tenant_id`) → `422`.

## Response `201`

```json
{
  "assessment_id": "0192…uuid7",
  "model_version": "release_risk_ci/3",
  "feature_schema_version": "1.0.0",
  "risk_score": 78,
  "risk_band": "HIGH",
  "probability": 0.61,
  "top_increasing": [{"feature": "log1p_src_churn", "features": ["src_churn", "log1p_src_churn"],
    "label": "Source lines changed", "value": 120, "contribution": 0.41,
    "text": "Source lines changed is in the top 10% of builds in the training data"}],
  "top_reducing": [],
  "history": {"finished_builds_used": 12, "cold_start": false},
  "details_url": "https://app.releaselens.dev/app/risk/0192…",
  "explanation_note": "Contributions show how the model arrived at its score (SHAP values in log-odds). They describe the model, not the causes of a failure.",
  "repository": "acme/payments", "commit_sha": "3f2a…", "event": "pull_request",
  "pull_request": 123, "triggered_at": "…", "created_at": "…"
}
```

- `risk_score` is the 0–100 percentile of the raw model score among the model's **validation**
  predictions (ADR-0012), from the quantile table in the model bundle's metadata.
- `risk_band` uses the cut-points stored in the bundle (validation recall 0.80 / 0.50 / 0.20 for
  MEDIUM / HIGH / CRITICAL). Nothing is hard-coded in the API: a new model brings its own.
- `probability` is non-null only if the bundle records that the ADR-0012 calibration gate passed
  on the test set. The current champion (`release_risk_ci/3`) passed; the cold-start model did
  not, so day-one assessments have `probability: null`.
- `top_increasing` / `top_reducing`: up to 3 SHAP contributions each (log-odds of the raw score).
  Features that share a label (`src_churn` + `log1p_src_churn`) are one item, summed. The text
  places the value in the **training** distribution. Explanations describe the **model**, not
  causation.
- `history.finished_builds_used`: the tenant's CI runs of this repository with an outcome that
  finished before `triggered_at` (all of them, not only the loaded window).
- `history.cold_start: true`: there were none, so the **no-history model** scored the change
  (below).
- `details_url`: `RL_WEB_BASE_URL` + `/app/risk/<id>`.

## Other answers

| Status | When |
|---|---|
| `200` | Idempotent replay: the same `(repository, commit_sha, event, pull_request)` was assessed by this tenant within `RL_RISK_IDEMPOTENCY_WINDOW_S` (600 s). The stored assessment is returned unchanged, whatever the new body says; no second Check Run or alert. Concurrent duplicates are serialised with a transaction-scoped advisory lock. |
| `401` / `403` | Missing or invalid key / key without `risk:write`. |
| `422` | Validation (`{"error": {"code": "validation_error", "details": [...]}}`), e.g. `unknown change facts: tr_status`. |
| `429` | Per-key rate limit. |
| `503 no_model_loaded` | The needed model is not loaded (`details.model` = `main` or `cold`). **A fabricated score is never returned.** `Retry-After: 60`. The GitHub Action does not retry this one. |

## How the score is computed

1. **History.** The API loads the repository's `ci_runs` that finished before `triggered_at`
   with an outcome (`passed`; `failed`/`errored` = failure, ADR-0005; `canceled`/`running`
   never count): the most recent `RL_RISK_HISTORY_LIMIT` (200) by finish time, plus the latest
   run that *started* before `triggered_at`, plus two exact totals counted in SQL (all eligible
   runs; runs since the last failure). Every history feature reads at most the last 20 runs,
   so with the totals the result is identical to using the whole history (tested on the real
   mart, below).
2. **Features.** `rl_core.features.online.compute_online(change_facts, history, triggered_at)`:
   the same functions the training mart used (ADR-0006).
3. **Model.** `cold_start` → the cold-start model, else the champion. Both are loaded once at
   startup; no MLflow call happens per request.
4. **Store** the assessment (features, change facts as sent, model version, raw score, score,
   band, probability, full SHAP vector + base value, history count, cold-start flag).
5. **Alerts.** Band ≥ HIGH and an alert rule of the repository's project (linked by
   `github_owner`/`github_repo`) with "risk alerts" on → a `notification_log` row in the same
   transaction (`rl_db.repositories.alerts.notify_risk`); the worker delivers it.
6. **Check Run.** If the repository's owner has a linked GitHub App installation, a Check Run is
   created after the response ([GitHub App](../integrations/GITHUB_APP.md)). A failure there
   is logged and never affects the assessment.

## Models served

| Role | Default URI (`RL_MODEL_URI` / `RL_COLD_MODEL_URI`) | Used when |
|---|---|---|
| main | `models:/release_risk_ci@champion` | the repository has ≥ 1 finished run |
| cold | `models:/release_risk_ci_cold@champion` | no finished run yet (day one) |

The cold-start model has the champion's family and hyper-parameters but **no history
features** (M4 found that masking history on the champion inflates scores; RESULTS.md §9). Its
real metrics: [`docs/ml/COLD_START_MODEL.md`](../ml/COLD_START_MODEL.md) (test PR-AUC 0.537 vs
base rate 0.279, ROC-AUC 0.712; calibration gate failed, so no probability).

Loading: a `models:/name@alias` (or `/version`) URI is resolved in the registry at
`RL_MLFLOW_TRACKING_URI`; the version's `risk_bundle` is downloaded once to
`RL_MODEL_CACHE_DIR/<name>/<version>/` with a SHA-256 manifest that is verified on every
start (a mismatch re-downloads). If the registry is down at startup the last cached version of
that alias is served. A plain directory path loads a bundle directly. Startup waits at most
`RL_MODEL_STARTUP_WAIT_S` (30 s) for the first attempt; failed loads retry every
`RL_MODEL_RETRY_INTERVAL_S` (60 s). A new champion is picked up on restart.
`GET /api/v1/risk/model` (read) lists what is loaded (version, checksum, probability exposed).

## Reads

Scope `read` key, or a browser session plus `X-RL-Org: <org id>` (same as deployments).

- `GET /api/v1/risk/assessments?repository=&commit_sha=&risk_band=&since=&limit=50&cursor=`:
  newest first, keyset cursor (`next_cursor`). `since` (ISO 8601 **with** offset, e.g.
  `2026-10-06T00:00:00Z`; a naive value is `422`) keeps assessments created at or after it.
- `GET /api/v1/risk/assessments/{id}`: the summary above plus `model_name`, `raw_score`,
  `branch`, `ci_run_url`, `previous_built_sha`, `extractor`, `features` (all 39, `null` =
  unknown), `change_facts` and `shap` (`unit`, `base_value`, `values`: the full vector;
  `base_value + Σ values` = the raw score's log-odds). Another tenant's id → `404`.

## CI runs: `POST /api/v1/ci-runs` / `GET /api/v1/ci-runs?repository=&commit_sha=`

History comes from the GitHub App webhooks (`workflow_run`, `check_suite`) or, without the App,
from CI reporting finished runs here. Scope **`risk:write`** (chosen over `deployments:write`:
runs only feed the risk model, and one narrow key per integration is ADR-0016's rule).

```json
{"repository": "acme/payments", "commit_sha": "…", "external_id": "gha:123456:1",
 "status": "failed", "started_at": "…", "finished_at": "…",
 "branch": "main", "pull_request": 12, "source": "api"}
```

`status` ∈ `passed | failed | errored | canceled | running`; `finished_at` is required for the
first three, forbidden for `running`, and never before `started_at`. Idempotent on
`(source, external_id)`: `201 created`, `200 updated` (a running run got its outcome), or
`200 duplicate` (already recorded; an outcome is never overwritten; the same `external_id`
reported by another source counts once). For GitHub Actions use
`gha:<run_id>:<run_attempt>`, the id the App records.

`GET /api/v1/ci-runs` (scope `read`, or a session plus `X-RL-Org`) lists runs newest start first
(`limit` ≤ 200, default 50). Give `repository`, `commit_sha` or both (neither → `422
validation_error`). `commit_sha` (7–64 hex, case-insensitive) is the CI outcome of one commit: 40+
characters match exactly; a shorter, abbreviated SHA (deployment events allow 7+) matches as a
prefix. Lookups by commit use the index `ix_ci_runs_tenant_commit_sha (tenant_id, commit_sha,
started_at DESC)` (migration `0006`).

## Latency (in process)

`python -m rl_api.risk.bench --model <uri>`: `compute_online` over a 200-run history plus
scoring with the full SHAP vector and explanation text, 500 calls after 20 warm-ups, XGBoost
with one thread (as served). Measured 2026-10-13 on an Apple M4 (10 cores, 16 GiB), Python
3.12.11, while the machine was under heavy unrelated load (load average ≈ 22):

| Model | features p50 / p95 | total p50 / p95 / p99 |
|---|---|---|
| `release_risk_ci/3` (34 features, 522 trees) | 0.51 / 0.97 ms | 4.26 / 7.92 / 9.22 ms |
| `release_risk_ci_cold/1` (26 features) | 0.61 / 1.06 ms | 2.64 / 4.04 / 4.87 ms |

Excludes HTTP, authentication and the database round trips (history query, insert). TreeSHAP
(`pred_contribs`) is most of the model time. Building the one-row `DMatrix` with XGBoost's
default thread pool cost ~7 ms per call, so it is built single-threaded; the booster's own thread
count made no material difference here (back-to-back runs: 8 threads p50 4.84 ms, 1 thread
6.13 ms, within this machine's noise), and one thread avoids oversubscribing cores under
concurrent requests. End-to-end load tests (k6) follow in M12.

## Parity

`ml/tests/test_serving_parity.py` (integration): for 200 deterministic builds of feature mart
`m3-run1`, the history is rebuilt from `mart_build_inputs` and `compute_online` (full history
**and** the bounded 200-run window with totals) equals the training feature row for all 39
features (rtol 1e-9, `null` = `null`). `packages/rl_db/tests/test_risk_repository.py` checks
that the SQL window and totals equal the pure function, ties included.
