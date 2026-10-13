# Blockers that need the owner

Things that need your accounts or manual steps. Each one has a working local fallback, so nothing
here stops development. Env var names are final.

| # | What | Local fallback in use | What you need to do |
|---|---|---|---|
| B1 | **GitHub App** (Check Runs, webhooks) | Signature-verified webhook fixtures in tests. The composite GitHub Action path works with just an API key. | Create a GitHub App (permissions: Checks RW, Pull requests R, Contents R, Metadata R; events: pull_request, check_suite, workflow_run, deployment_status). Then set `RL_GITHUB_APP_ID`, `RL_GITHUB_APP_PRIVATE_KEY_FILE`, `RL_GITHUB_WEBHOOK_SECRET` in `infra/compose/.env`. Detailed steps arrive with M5. |
| B2 | **Stripe test keys** | Signed webhook fixtures and a fake gateway in tests. Without keys, checkout/portal/webhook answer `503 billing_not_configured`. | In Stripe **test mode**, create a product "Team" with a $99/month price. Set `RL_STRIPE_SECRET_KEY=sk_test_…` (live keys are refused), `RL_STRIPE_WEBHOOK_SECRET=whsec_…` and `RL_STRIPE_PRICE_TEAM=price_…`, and enable the Customer Portal. Step by step: [`docs/billing/BILLING.md`](billing/BILLING.md#local-test-mode-setup-owner-b2). |
| B3 | **Slack incoming webhook** | E-mail alerts (Mailpit in dev) and the notification log; tests mock Slack. | Create an incoming webhook and set it on a project's alert rule (`slack_webhook_url`, Team plan; stored AES-GCM encrypted, never shown again). Outside dev/test only `https://hooks.slack.com/services/…` is accepted. |
| B4 | **AWS S3** (optional) | `pgsty/silo` local S3 (ADR-0009). | Only if you want real S3: set `RL_S3_ENDPOINT` (empty for AWS), `RL_S3_ACCESS_KEY`, `RL_S3_SECRET_KEY`, `RL_S3_REGION`. I will not provision anything that costs money. |
| B5 | **GitHub OAuth app** (sign-in with GitHub) | Email + password auth works locally (Mailpit for verification mail). Without the app, the GitHub endpoints answer `503 github_oauth_not_configured`. | Create an OAuth app with callback `http://localhost:28000/api/v1/auth/github/callback` (the API's callback; override with `RL_GITHUB_OAUTH_REDIRECT_URI`). Set `RL_GITHUB_OAUTH_CLIENT_ID`, `RL_GITHUB_OAUTH_CLIENT_SECRET`. |
