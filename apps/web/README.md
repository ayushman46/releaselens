# @releaselens/web

The ReleaseLens web app, built with the Next.js App Router. M11a adds the design system, the
dashboard shell and the marketing site on top of the M1 skeleton.

| Route | What it does |
|---|---|
| `/` | Landing page: the two questions, the PREDICT → LEARN loop, integrations, methodology, pricing teaser |
| `/pricing`, `/changelog` | Plans (planned launch pricing) and the milestone changelog |
| `/docs`, `/docs/[slug]` | Docs index and outline pages (Quick start, GitHub setup, Deployment API, OTel setup, Methodology, Limitations). TSX until MDX lands |
| `/status` | Live API health. The browser calls `GET /healthz` and `GET /readyz` on the API (TanStack Query, polled every 15 s), validates the responses with zod, and shows each dependency check (status, latency, error) |
| `/app`, `/app/*` | Dashboard shell (sidebar, top bar, ⌘K / Ctrl+K command menu). Overview, Deployments, Regressions, Risk analysis, Services, Service map, Analytics, Model, Settings, each with an empty state that explains how to connect data. No sample rows |
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

To add more shadcn components later, run `pnpm dlx shadcn@latest add <name>`. `components.json` is already set up.

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
