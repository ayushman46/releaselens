# @releaselens/web

The ReleaseLens web app, built with the Next.js App Router. M11a added the design system, the
dashboard shell and the marketing site; M11b connects the dashboard to the API with real data only
(no sample rows, ever): sign-in, organizations, deployments, the deployment evidence page,
regressions, services, the service map, risk assessments, analytics, the model and settings.

| Route | What it does |
|---|---|
| `/` | Landing page: the two questions, the PREDICT → LEARN loop, integrations, methodology, pricing teaser |
| `/pricing`, `/changelog` | Plans (planned launch pricing) and the milestone changelog |
| `/docs`, `/docs/[slug]` | Docs index and outline pages (Quick start, GitHub setup, Deployment API, OTel setup, Methodology, Limitations). TSX until MDX lands |
| `/status` | Live API health. The browser calls `GET /healthz` and `GET /readyz` on the API (TanStack Query, polled every 15 s), validates the responses with zod, and shows each dependency check (status, latency, error) |
| `/login`, `/signup`, `/verify-email`, `/forgot-password`, `/reset-password`, `/invites/accept` | Auth pages. E-mailed tokens are read from the URL fragment (`#token=`). "Continue with GitHub" checks the API first and says "not configured" on `503` |
| `/app` | Overview: deployments, active regressions, risk assessments this week and service health, with setup steps until data arrives |
| `/app/onboarding` | First run: create the organization, then connect data |
| `/app/deployments`, `/app/deployments/[id]` | Deployment list (filters in the URL, cursor pagination, saved views in localStorage) and the evidence page: prediction vs actual, pre-deploy risk with the CI result as a separate signal, analysis windows timeline with onset, probable origin, blast radius (React Flow), before/after table with CIs and sample sizes, p95 latency per minute (Recharts) |
| `/app/regressions`, `/app/services`, `/app/service-map` | Regressed/warning deployments, the service catalog (approximate p95) and the service graph with a details panel |
| `/app/risk`, `/app/risk/[id]` | Risk assessments with SHAP explanations (top signals and the full vector) |
| `/app/analytics`, `/app/model` | Historical CI findings (with CI error bars and the association-not-causation caveats) and the champion model (metrics, confusion matrix, reliability, global SHAP, bands, model-card quotes). Static data copied from `docs/` by `pnpm sync:data` |
| `/app/settings/*` | General, Members, API keys (secret shown once), Alerts (Slack URL write-only), Audit log (filters, CSV export), Billing (Stripe Checkout; "Billing not configured" on `503`) |
| `/api/health` | Liveness for the container healthcheck: `200 {"status":"ok"}`, no dependencies checked |

Layout: `src/app/(marketing)` and `src/app/(app)/app` are route groups with their own layouts.
Components: `src/components/ui` (generic primitives: Button, Badge, Tabs, Tooltip, Dialog, DataTable,
CodeBlock, EmptyState, ErrorState, PageHeader, KeyValueList, Callout, Kbd, Skeleton),
`src/components/rl` (StatusPill, RiskBadge, SeverityBadge, StatCard, DemoDataBanner),
`src/components/shell` (sidebar, top bar, command menu) and `src/components/marketing`.
Tabs, Tooltip, Dialog and DataTable are dependency-free and follow the WAI-ARIA patterns.

Stack: Next 16.3.8, React 19.2.8, TypeScript 5 (strict), Tailwind CSS v4, shadcn/ui (new-york, zinc;
components in `src/components/ui`), TanStack Query, zod, Vitest and Testing Library. The theme is dark-first:
`.dark` on `<html>` is the default, and the toggle stores the light-theme choice in `localStorage`.
Fonts come from the system font stack, so builds never download fonts.

Design tokens live in `src/app/globals.css` as hex values for both themes: surfaces, borders, text,
the brand accent, and six status families (healthy, warning, high, critical, unknown, info). Risk and
severity bands map onto them in `src/lib/tones.ts`. `src/lib/contrast.test.ts` parses the stylesheet and
fails if any text pair drops below 4.5:1 or any UI pair (focus ring, control borders, brand indicator)
below 3:1. `--border` is a decorative separator and is deliberately below 3:1. A status colour is never
the only signal: StatusPill, RiskBadge and SeverityBadge always render an icon and a text label.

## Scripts

This directory is its own pnpm project with its own `pnpm-lock.yaml`. pnpm 11 settings live in
`pnpm-workspace.yaml`.

| Command | Purpose |
|---|---|
| `pnpm install` | Install dependencies (CI and Docker use `--frozen-lockfile`) |
| `pnpm dev` | Dev server on port 3000 (`pnpm dev -p 23000` to use the compose host port, see below) |
| `pnpm build` | Production build to `.next/standalone` (`output: "standalone"`) |
| `pnpm start` | Serve the production build locally on port 3000. Next warns that `next start` and standalone don't mix. The warning is harmless here, and the container runs `node server.js` instead |
| `pnpm lint` | ESLint 9 (flat config, `eslint-config-next`) |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm test` | Vitest (jsdom), once |
| `pnpm gen:api` | Dump the FastAPI OpenAPI spec without a server (`uv run python -c … create_app(...).openapi()`) into `openapi/releaselens.json`, then generate `src/lib/api/schema.d.ts` with openapi-typescript. `pnpm gen:api:types` skips the Python step |
| `pnpm sync:data` / `pnpm check:data` | Copy `docs/analytics/findings.json`, `docs/ml/results/*.json` and model-card excerpts into `src/data/` (the Docker context has no `docs/`) / fail if the copies are stale |
| `pnpm test:e2e` | Playwright against a running stack (browsers are installed separately), see [docs/web/E2E.md](../../docs/web/E2E.md) |

To add more shadcn components later, run `pnpm dlx shadcn@latest add <name>`. `components.json` is already set up.

## API client and sessions

`src/lib/api/client.ts` wraps openapi-fetch with the types from `schema.d.ts`: every request is
sent with `credentials: "include"`, mutations carry `X-CSRF-Token` (from the `rl_csrf` cookie, or the
`csrf_token` of the last login/refresh), tenant-implicit routes get `X-RL-Org` with the active
organization, a `401` triggers one refresh (serialised in the tab and across tabs with
`navigator.locks`) and one retry, and errors are parsed from the `{"error": {...}}` envelope into
`ApiError`. A failed refresh sends the user to `/login`; `402 plan_limit_reached` opens the upgrade
dialog. `src/proxy.ts` redirects `/app/*` to `/login` when no session cookie is present (set
`RL_WEB_SESSION_COOKIE_CHECK=0` when the API's cookies are not visible to the web host).

## Environment

| Variable | Default | Notes |
|---|---|---|
| `NEXT_PUBLIC_RL_API_URL` | `http://localhost:28000` | Base URL of the API **as seen from the browser**. It is inlined at **build time**, so changing it needs a rebuild (Docker: `--build-arg`). Values that are blank or not http(s) fall back to the default |

The browser calls the API cross-origin, so the API's `RL_CORS_ORIGINS` must include the web
origin. The default is `http://localhost:23000`. When `/status` shows "API unreachable" during
local development, check CORS first:

- run the dev server on the compose host port: `pnpm dev -p 23000`, or
- add `http://localhost:3000` to `RL_CORS_ORIGINS` on the API.

## Ports

| Where | Port |
|---|---|
| Container (`PORT`, `EXPOSE`) | 3000 |
| Host, local compose (`web` service) | 23000 |
| API, host (`NEXT_PUBLIC_RL_API_URL` default) | 28000 |

## Docker

The build context is this directory:

```sh
docker build -t rlens/web \
  --build-arg NEXT_PUBLIC_RL_API_URL=http://localhost:28000 \
  apps/web
```

The build is multi-stage on `node:22-alpine`, with pnpm via corepack (the version comes from
`packageManager`). The runtime image holds only `.next/standalone`, `.next/static` and `public`. It
runs as the unprivileged `node` user with `HOSTNAME=0.0.0.0` and `PORT=3000`. The image's
`HEALTHCHECK` calls `GET /api/health`. A compose healthcheck can do the same:
`wget -q -O /dev/null http://127.0.0.1:3000/api/health`.
