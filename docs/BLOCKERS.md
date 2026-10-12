# Blockers that need the owner

Things that need your accounts or manual steps. Each one has a working local fallback, so nothing
here stops development. Env var names are final.

| # | What | Local fallback in use | What you need to do |
|---|---|---|---|
| B1 | **GitHub App** (Check Runs, webhooks) | Signature-verified webhook fixtures in tests. The composite GitHub Action path works with just an API key. | Create a GitHub App (permissions: Checks RW, Pull requests R, Contents R, Metadata R; events: pull_request, check_suite, workflow_run, deployment_status). Then set `RL_GITHUB_APP_ID`, `RL_GITHUB_APP_PRIVATE_KEY_FILE`, `RL_GITHUB_WEBHOOK_SECRET` in `infra/compose/.env`. Detailed steps arrive with M5. |
| B2 | **Stripe test keys** | Signed webhook fixtures. Billing UI runs in "Stripe not configured" mode. | In Stripe **test mode**, create a product "Team" with a $99/month price. Set `RL_STRIPE_SECRET_KEY=sk_test_…`, `RL_STRIPE_WEBHOOK_SECRET=whsec_…` and `RL_STRIPE_PRICE_TEAM=price_…`. Arrives with M10. |
| B3 | **Slack incoming webhook** | Notifications go to Mailpit (email) and the notification log. | Create an incoming webhook and set its URL per project in Settings → Integrations (stored encrypted). Arrives with M10. |
| B4 | **AWS S3** (optional) | `pgsty/silo` local S3 (ADR-0009). | Only if you want real S3: set `RL_S3_ENDPOINT` (empty for AWS), `RL_S3_ACCESS_KEY`, `RL_S3_SECRET_KEY`, `RL_S3_REGION`. I will not provision anything that costs money. |
| B5 | **GitHub OAuth app** (sign-in with GitHub) | Email + password auth works locally (Mailpit for verification mail). | Create an OAuth app with callback `http://localhost:23000/api/auth/github/callback`. Set `RL_GITHUB_OAUTH_CLIENT_ID`, `RL_GITHUB_OAUTH_CLIENT_SECRET`. Arrives with M10. |
