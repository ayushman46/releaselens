# GitHub App: Check Runs and CI history (M5)

The [GitHub Action](GITHUB_ACTION.md) works with only an API key. The GitHub App adds two
things:

1. **Check Runs.** Every risk assessment of a repository the App is installed on appears on the
   commit / pull request as a Check Run titled `ReleaseLens: Risk HIGH, 78/100`, with the top
   signals both ways, the history used, the honesty disclaimer and a link to the assessment.
   Its conclusion is always **neutral**: the score is advisory (the model card rules out
   automatic merge blocking; teams that want a gate use the Action's `fail-on-band`).
2. **CI history without scripting.** `workflow_run` and `check_suite` webhooks become the
   tenant's `ci_runs`, which feed the model's history features (previous build failed, recent
   failure rates, …). Without them a repository stays on the cold-start model.

This is blocker **B1** in [`docs/BLOCKERS.md`](../BLOCKERS.md): the App must be created under
your GitHub account. Until then every path below is tested with signed fixture deliveries and a
mocked GitHub API, and the webhook answers `503 github_app_not_configured`.

## 1. Create the App (owner)

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**:

| Setting | Value |
|---|---|
| Homepage URL | your ReleaseLens web URL |
| Webhook URL | `https://<api host>/api/v1/integrations/github/webhook` (for local tests, a tunnel to `http://localhost:28000`) |
| Webhook secret | a long random string, e.g. `openssl rand -hex 32` → `RL_GITHUB_WEBHOOK_SECRET` |
| Repository permissions | **Checks: Read & write**, **Pull requests: Read**, **Contents: Read**, **Metadata: Read** (mandatory), **Actions: Read** (for `workflow_run`), **Deployments: Read** (for `deployment_status`) |
| Subscribe to events | **Pull request**, **Check suite**, **Workflow run**, **Deployment status** |
| Where can it be installed | "Only on this account" unless you operate ReleaseLens for others |

After creating it: note the **App ID**, then **Generate a private key** (a `.pem` download).
Store the PEM outside the repository (`*.pem` is git-ignored anyway) with mode `0600`.

## 2. Configure the API

| Variable | Value |
|---|---|
| `RL_GITHUB_APP_ID` | the numeric App ID |
| `RL_GITHUB_APP_PRIVATE_KEY_FILE` | path of the PEM **as the API process sees it** |
| `RL_GITHUB_WEBHOOK_SECRET` | the webhook secret |
| `RL_GITHUB_API_URL` | optional, default `https://api.github.com` (GitHub Enterprise Server: `https://<host>/api/v3`) |
| `RL_GITHUB_CHECK_RUN_NAME` | optional, default `ReleaseLens risk` |

With compose, put the three values in `infra/compose/.env` and mount the key read-only into the
`api` service with a `compose.override.yaml` (not committed):

```yaml
services:
  api:
    volumes: ["/secure/path/releaselens-app.pem:/run/secrets/github-app.pem:ro"]
```

and set `RL_GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github-app.pem`. The key is read once at
startup; an unreadable or non-RSA key stops the API (fail fast) rather than failing every Check
Run later. Check Runs need the App ID and key; the webhook needs only the secret.

## 3. Install the App and link the installation (operator)

1. Install the App on the organization (or user) that owns the repositories: the App's public
   page → **Install** → choose all or selected repositories.
2. Find the **installation id**: GitHub → the organization's Settings → GitHub Apps →
   *Configure* next to the App; it is the number at the end of the URL
   (`…/settings/installations/12345678`).
3. Link it to the ReleaseLens organization (operator CLI, against the API's database):

   ```bash
   uv run python -m rl_api.admin link-github-installation \
       --org acme --installation-id 12345678 --account acme-gh-org
   ```

   `--account` is the GitHub login the App is installed on: Check Runs are created for
   repositories `<account>/*` assessed by that ReleaseLens organization.

Linking is an operator action on purpose. Anyone can install a public App, so an installation id
proves nothing about which ReleaseLens organization it belongs to; a self-service flow must first
verify that the signed-in user administers that GitHub account (GitHub's user-to-server
`GET /user/installations`). An installation belongs to one organization: linking it to a second
one is refused.

4. Optional, for alerts and deployment events: in the web app, set the project's GitHub
   repository (`github_owner` / `github_repo`). Risk alerts of rules with "risk alerts" on, and
   `deployment_status` events, use it.

## 4. What the webhook does

`POST /api/v1/integrations/github/webhook`:

1. No `RL_GITHUB_WEBHOOK_SECRET` → `503 github_app_not_configured`.
2. `X-Hub-Signature-256` must equal `sha256=` + HMAC-SHA256(secret, raw body), compared in
   constant time, before anything is parsed; otherwise `401 invalid_signature`.
3. `X-GitHub-Delivery` is recorded once (`webhook_deliveries`); a redelivery answers
   `200 {"status": "duplicate"}` and changes nothing.
4. `installation.id` → organization (`github_installations`). Unknown or suspended
   installations are acknowledged (`202`, `ignored`).
5. The event, in the organization's context and the same transaction:

| Event | Effect |
|---|---|
| `workflow_run` (`requested`, `in_progress`, `completed`) | `ci_runs` row `gha:<run id>:<attempt>`: running, then passed / failed / errored / canceled. Only runs triggered by `push`, `pull_request`, `pull_request_target`, `merge_group` count as builds (scheduled and manual workflows are ignored). |
| `check_suite` (`completed`) | `ci_runs` row `check_suite:<id>` for **other** CI apps. GitHub Actions suites are skipped (covered by `workflow_run`), and so are the App's own suites. |
| `pull_request` | the PR's state (head/base SHA, state, merged, draft). No title, body or author is stored. |
| `deployment_status` (`success`) | a `deployment.completed` event through the outbox, if the repository's project has exactly one service (service, environment, `version` = commit SHA, `labels.source = github_app`, idempotent per status id). |
| `installation` (`deleted`, `suspend` / `unsuspend`) | suspends / resumes the link. |
| `ping` | `200 {"status": "pong"}` |

Conclusions map to the training label (ADR-0005): `success` → passed, `failure` → failed,
`timed_out` / `startup_failure` → errored, anything else → canceled (never counts as an outcome).
A malformed payload is acknowledged with `ignored:malformed:…` and not applied.

Fidelity note: TravisTorrent had one build per push. A repository with several push/PR
workflows records one CI run per workflow run, so its history features see more, shorter
"builds" than the training data did. The feature-fidelity measurement on real repositories
(open, see the M5 report) quantifies this; until then, prefer installing on repositories whose
main CI is one workflow.

## 5. Check Run details

After a `201` assessment of `owner/repo`, if the organization has an active installation whose
account is `owner`: an installation token is requested with an RS256 JWT
(`iss` = App ID, 9-minute expiry, backdated 60 s; tokens are cached until 2 minutes before they
expire) and `POST /repos/{owner}/{repo}/check-runs` is called with `head_sha` = the assessed
commit, `status: completed`, `conclusion: neutral`, `external_id` = the assessment id and
`details_url` = the assessment page (https only). This runs after the response is sent; a GitHub
error is logged and never changes the assessment. Idempotent replays post nothing.
