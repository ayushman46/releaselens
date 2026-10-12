# Risk assessments API (contract, M5)

`POST /api/v1/risk/assessments` — scope `risk:write` (header `Authorization: Bearer <key>` or
`x-releaselens-key: <key>`). The tenant comes **only** from the key.

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

- `change_facts` keys ⊆ `rl_core.features.change.BUILD_INPUT_FIELDS` (definitions:
  `docs/data/FEATURE_DEFINITIONS.md`). Unknown keys → `422`. Missing keys → treated as unknown
  (NaN), never as 0.
- `event` ∈ `push | pull_request`. `triggered_at` must be timezone-aware.
- Idempotency: the same `(repository, commit_sha, event, pull_request)` within 10 minutes returns
  the existing assessment (`200`) instead of creating a new one (`201`).

## Response `201`

```json
{
  "assessment_id": "0192…uuid7",
  "model_version": "release_risk_ci/7",
  "feature_schema_version": "1.0.0",
  "risk_score": 78,
  "risk_band": "HIGH",
  "probability": null,
  "top_increasing": [{"feature": "log1p_src_churn", "label": "Source lines changed", "value": 120, "contribution": 0.41, "text": "Source lines changed is in the top 10% of builds in the training data"}],
  "top_reducing": [],
  "history": {"finished_builds_used": 12, "cold_start": false},
  "details_url": "https://app.releaselens.dev/app/risk/0192…"
}
```

- `risk_score` is the 0–100 percentile of the raw model score among validation predictions
  (ADR-0012). `probability` is non-null only if the calibration gate passed.
- Explanations describe the **model** (SHAP contributions), not causation.
- `503` when no champion model is loaded. A fabricated score is never returned.
