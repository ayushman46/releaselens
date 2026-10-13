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
API key. Sign up in the web app first, then run the demo with your address:

```sh
DEMO_MEMBER_EMAIL=you@example.test demo/scenarios/run_demo.sh
```

The script then runs `python -m rl_api.admin add-member --org <demo slug> --email <you> --role
owner`, so the demo organization appears in your organization switcher. The user must already
exist (the CLI never creates accounts); an existing membership just gets the role.

## Analysis routes and browser sessions

The deployment page's analysis and the Regressions, Services and Service map pages read
`GET /api/v1/deployments/{id}/analysis`, `/api/v1/regressions`, `/services` and `/service-map`.
They accept a browser session plus `X-RL-Org` (`require_tenant_read`), like `/api/v1/deployments`
and `/api/v1/risk/*`.

## CI

Run it in a job that starts the compose stack, installs Chromium with
`pnpm exec playwright install --with-deps chromium`, and keeps `playwright-report/` and
`test-results/` (both git-ignored) as artifacts. Traces are kept on failure.
