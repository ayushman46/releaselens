import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against a running stack (docs/web/E2E.md). Nothing is started here: point
 * E2E_BASE_URL at the web app and E2E_API_URL at the API. Browsers are installed separately
 * (`pnpm exec playwright install chromium`), so `pnpm install` stays small.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:23000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
