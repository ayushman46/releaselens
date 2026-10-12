# ReleaseLens CI risk (GitHub Action)

A composite action that estimates how likely a change is to **fail CI**, before the result is
known. It computes aggregate facts about the change from git inside your runner (lines changed,
files touched, tests added, codebase size, file hotness, team size), sends only those counts to
the ReleaseLens API, and writes the score to the job summary and the step outputs.

> This is a CI-failure risk estimate from historical patterns, not a production outage
> probability.

Setup, privacy details and how every fact is computed:
[`docs/integrations/GITHUB_ACTION.md`](../../docs/integrations/GITHUB_ACTION.md).

## Quick start

```yaml
permissions:
  contents: read

jobs:
  risk:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0 # full history
      - id: risk
        uses: ayushman46/releaselens/integrations/github-action@<full-commit-sha> # v0.1.0
        with:
          api-key: ${{ secrets.RELEASELENS_API_KEY }} # scope risk:write
```

A complete workflow (pull requests + pushes) is in [`examples/releaselens.yml`](examples/releaselens.yml).

## Inputs

| Input | Default | Description |
|---|---|---|
| `api-key` | required | API key with scope `risk:write`. Always pass it from a secret. If it is empty (for example on pull requests from forks, which get no secrets), the action skips with a notice. |
| `api-url` | `https://api.releaselens.dev` | API base URL. The default is a placeholder for the hosted service; for a local stack use `http://localhost:28000` (self-hosted runner). |
| `fail-on-band` | `none` | `none`, `HIGH` or `CRITICAL`. When set, the step fails if the band is at or above it. It also fails when no score can be obtained (fail closed). With `none`, API problems never fail the build. |
| `language` | `java` | Primary language. `java` is supported in v0.1. |
| `src-globs` | empty | Comma- or newline-separated globs (`**` crosses directories) that replace the built-in extension table for source files. This deviates from the definitions the model was trained with. |
| `test-globs` | empty | Same, replacing the test-file rules (Java: `test/` or `tests/` directories, `*Test.java`, `*Tests.java`, `Test*.java`). |
| `timeout-seconds` | `60` | Total time budget for the API call, retries included. |
| `previous-sha` | empty | Previous built commit. It overrides the push `before` SHA or the PR base SHA. |
| `working-directory` | `.` | Path of the git checkout. |

## Outputs

| Output | Description |
|---|---|
| `risk-score` | 0–100 percentile risk score. Empty when there is no score. |
| `risk-band` | `LOW`, `MEDIUM`, `HIGH` or `CRITICAL`. Empty when there is no score. |
| `assessment-id` | ReleaseLens assessment id. |
| `details-url` | Link to the assessment in ReleaseLens. |
| `status` | `ok`, `unavailable` (API has no model: HTTP 503), `error` or `skipped` (no API key). |

## What leaves the runner

Only the request body of `POST /api/v1/risk/assessments`: repository name, commit SHAs, PR
number, branch name, event, trigger time, CI run URL, extractor version, and about 20 **aggregate
numbers and booleans** (`change_facts`). No source code, file contents, file paths, diffs, commit
messages or author identities are sent. Author emails are compared only inside the runner, to
count the team. The API key goes only in the `Authorization` header to `api-url`. It is masked in
logs, never passed on a command line, and redirects are not followed.

## Behaviour

- **History depth.** Use `fetch-depth: 0`. With a shallow clone the action warns, still sends the
  facts it can prove (codebase size and test density always need only the head tree), and sends
  history facts as `null` (unknown), never as guesses.
- **No model yet (HTTP 503).** You get a notice and `status=unavailable`. The build does not fail
  unless `fail-on-band` is set.
- **Other API errors.** Network errors, HTTP 429 and 5xx are retried with exponential backoff
  within `timeout-seconds`. After that you get a warning, `status=error`, and the build does not
  fail unless `fail-on-band` is set.
- **Runner requirements.** `bash`, `git`, `curl` and Python 3.9 or newer (standard library only;
  nothing is installed). These are on all GitHub-hosted Linux and macOS runners.

## Run the extractor locally

```bash
python3 integrations/github-action/extract_change_facts.py \
  --repo . --head HEAD --previous HEAD~1 --language java \
  --diagnostics-out /tmp/rl-diagnostics.json
```

It prints the API request body. `--diagnostics-out` says why any fact is `null`. Run
`--help` for every option (work caps, blob-count cache, glob overrides).

## Development

```bash
uv run --no-sync pytest integrations/github-action/tests
uv run --no-sync ruff check integrations/github-action && uv run --no-sync ruff format integrations/github-action
uv run --no-sync mypy integrations/github-action
```

The tests build throwaway git repositories with fixed dates and assert exact facts. They also
drive the shell steps against a local HTTP stub and check that the key never reaches the logs.
`ruff.toml` lints this directory for Python 3.9, the oldest interpreter the scripts support.
