# syntax=docker/dockerfile:1
#
# ReleaseLens web (Next.js standalone server).
# Build context: apps/web
#   docker build -t rlens/web --build-arg NEXT_PUBLIC_RL_API_URL=http://localhost:28000 apps/web

ARG NODE_IMAGE=node:22-alpine

# ---- base: Node + pnpm (version from package.json "packageManager", via corepack) ----
FROM ${NODE_IMAGE} AS base
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
RUN corepack enable

# ---- deps: install from the lockfile only (cached unless manifests change) ----
FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=rlens-web-pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store

# ---- builder: next build -> .next/standalone ----
FROM base AS builder
# NEXT_PUBLIC_* values are inlined into the client bundle at build time.
# This is the API URL as seen from the user's browser (host port in local compose).
ARG NEXT_PUBLIC_RL_API_URL=http://localhost:28000
ENV NEXT_PUBLIC_RL_API_URL=${NEXT_PUBLIC_RL_API_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# public/ is optional in git; make sure the runtime COPY below always has a source.
RUN mkdir -p public && pnpm build

# ---- runner: minimal runtime, non-root ----
FROM ${NODE_IMAGE} AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app

# The official image ships an unprivileged "node" user (uid 1000).
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node
EXPOSE 3000

# Liveness only (no dependencies): GET /api/health -> {"status":"ok"}. busybox wget is in alpine.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:3000/api/health || exit 1

CMD ["node", "server.js"]
