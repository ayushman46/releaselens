# Web end-to-end tests (Playwright)

`apps/web/tests/e2e/demo.spec.ts` drives the spec's demo flow (§14.3) in a real browser against a
running stack: sign up → confirm the e-mail → sign in → onboarding (create the organization) →
the demo deployment shows a regression whose **probable origin** is `payment`.

`@playwright/test` 1.63.0 is a dev dependency, but **browsers are not installed by `pnpm install`**
(they are ~150 MB per engine). The spec type-checks with the rest of the app (`pnpm typecheck`);
running it needs the browser once.

## Run it

```sh
# 1. The stack: API on :28000, web on :23000, Mailpit on :28025 (infra/compose, `make up` or similar).
#    The web build must point at the API: NEXT_PUBLIC_RL_API_URL=http://localhost:28000.

# 2. Demo data (optional, ~25 min): a real payment v1 -> v2 regression.
SCENARIO=payment demo/scenarios/run_demo.sh      # prints the deployment URL at the end

# 3. Browsers, once (not done in CI images that already have them):
cd apps/web
pnpm exec playwright install chromium

# 4. Run.
E2E_BASE_URL=http://localhost:23000 \
E2E_API_URL=http://localhost:28000 \
E2E_MAILPIT_URL=http://localhost:28025 \
E2E_EMAIL=you@example.com E2E_PASSWORD='…' \
pnpm test:e2e
```

| Variable | Default | Used for |
|---|---|---|
| `E2E_BASE_URL` | `http://localhost:23000` | the web app under test (Playwright `baseURL`) |
| `E2E_API_URL` | `http://localhost:28000` | `/healthz` reachability check |
| `E2E_MAILPIT_URL` | `http://localhost:28025` | reading the verification e-mail (`/api/v1/search`, `/api/v1/message/{id}`) |
| `E2E_EMAIL`, `E2E_PASSWORD` | unset | an account that is a **member of the organization holding the demo data**; without them the demo test is skipped (the sign-up test still runs) |
| `E2E_DEMO_DEPLOYMENT_ID` | unset | open this deployment directly instead of picking the newest `payment` row on `/app/regressions` |

The sign-up test creates a fresh `e2e-<timestamp>@example.test` account each run (Mailpit catches
the e-mail; nothing leaves the machine).

## Getting the demo data into your account's organization

`run_demo.sh` creates its own organization with the admin CLI (`rl_api.admin create-org`) and an
API key. That organization has no members, so no one can invite you into it, and the admin CLI has
no "add member" command yet. Until the backend adds one (or lets the script reuse an existing key),
either:

* add your user to the demo organization with a membership row in the local database, or
* create a key with `deployments:write`, `telemetry:write`, `risk:write` and `read` in
  **Settings → API keys** of your own organization and run the demo services with
  `RL_DEMO_INGEST_KEY=<that key>`, skipping step 2 of the script (a small `run_demo.sh` change,
  owned by the backend).

## Known limitation (backend)

The deployment page's analysis (timeline, before/after, probable origin, blast radius, latency) comes
from `GET /api/v1/deployments/{id}/analysis`, and the Regressions, Services and Service map pages
from `GET /api/v1/regressions`, `/services` and `/service-map`. At the time of writing those four
routes accept **API keys only** (`require_scope("read")`), so a browser session gets `401` and the
pages show "not available to browser sessions yet". The demo test therefore passes only once those
routes use `require_tenant_read` like `/api/v1/deployments` and `/api/v1/risk/*` already do.

## CI

Run it in a job that starts the compose stack, installs Chromium with
`pnpm exec playwright install --with-deps chromium`, and keeps `playwright-report/` and
`test-results/` (both git-ignored) as artifacts. Traces are kept on failure.
