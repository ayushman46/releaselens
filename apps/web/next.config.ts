import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the Docker runtime image (see Dockerfile).
  output: "standalone",
  // apps/web has its own lockfile; pin the tracing root so the standalone
  // output is laid out at .next/standalone/server.js, not nested by repo path.
  outputFileTracingRoot: __dirname,
  poweredByHeader: false,
  // Next 16.3 `next dev` otherwise writes AGENTS.md + CLAUDE.md into apps/web.
  // Opted out to keep the tree clean; set to true if you want those files.
  agentRules: false,
  reactStrictMode: true,
};

export default nextConfig;
