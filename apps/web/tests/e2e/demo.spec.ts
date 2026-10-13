import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The spec's demo flow (§14.3): sign up → confirm e-mail → onboarding → (the backend's demo
 * script produces a real payment regression) → the deployment page shows the regression with
 * "payment" as the probable origin.
 *
 * Environment (see docs/web/E2E.md):
 *   E2E_BASE_URL      web app            (default http://localhost:23000)
 *   E2E_API_URL       API                (default http://localhost:28000)
 *   E2E_MAILPIT_URL   Mailpit web/API    (default http://localhost:28025; local compose)
 *   E2E_EMAIL / E2E_PASSWORD   an account that is a member of the organization holding the
 *                     demo data (`demo/scenarios/run_demo.sh`); the demo test is skipped without
 *   E2E_DEMO_DEPLOYMENT_ID     optional: open this deployment instead of finding the latest
 *                     payment regression in the list
 */

const API_URL = (process.env.E2E_API_URL ?? "http://localhost:28000").replace(/\/+$/, "");
const MAILPIT_URL = (process.env.E2E_MAILPIT_URL ?? "http://localhost:28025").replace(/\/+$/, "");
const DEMO_EMAIL = process.env.E2E_EMAIL;
const DEMO_PASSWORD = process.env.E2E_PASSWORD;
const DEMO_DEPLOYMENT_ID = process.env.E2E_DEMO_DEPLOYMENT_ID;

/** Polls Mailpit for the newest message to `to` and returns the first link matching `pattern`. */
async function linkFromMail(request: APIRequestContext, to: string, pattern: RegExp): Promise<string> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const search = await request.get(`${MAILPIT_URL}/api/v1/search`, { params: { query: `to:"${to}"` } });
    if (search.ok()) {
      const body = (await search.json()) as { messages?: Array<{ ID: string }> };
      const id = body.messages?.[0]?.ID;
      if (id) {
        const message = await request.get(`${MAILPIT_URL}/api/v1/message/${id}`);
        const { Text = "", HTML = "" } = (await message.json()) as { Text?: string; HTML?: string };
        const match = `${Text}\n${HTML}`.match(pattern);
        if (match) return match[0];
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`no e-mail to ${to} with a link matching ${pattern} arrived in Mailpit (${MAILPIT_URL})`);
}

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL(/\/app(\/|$)/);
}

test.describe("ReleaseLens demo flow", () => {
  test("the API is reachable", async ({ request }) => {
    const health = await request.get(`${API_URL}/healthz`);
    expect(health.ok()).toBe(true);
  });

  test("sign up, confirm the e-mail, sign in and create an organization", async ({ page, request }) => {
    const email = `e2e-${Date.now()}@example.test`;
    const password = `correct horse ${Date.now()} staple`;

    await page.goto("/signup");
    await page.getByLabel("Work e-mail").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();

    const link = await linkFromMail(request, email, /https?:\/\/[^\s"'<>]+\/verify-email#token=[A-Za-z0-9_-]+/);
    await page.goto(new URL(link).pathname + new URL(link).hash);
    await expect(page.getByRole("heading", { name: "E-mail confirmed" })).toBeVisible();

    await signIn(page, email, password);
    await expect(page).toHaveURL(/\/app\/onboarding$/);
    await page.getByLabel("Organization name").fill(`E2E ${Date.now()}`);
    await page.getByRole("button", { name: "Create and continue" }).click();
    await expect(page.getByRole("heading", { name: "Connect your data" })).toBeVisible();

    await page.goto("/app");
    await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
    // A new organization shows setup guidance, never sample rows.
    await expect(page.getByRole("heading", { name: "Connect ReleaseLens" })).toBeVisible();

    await page.getByRole("button", { name: /^Account:/ }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);
  });

  test("the demo deployment shows a regression with probable origin payment", async ({ page }) => {
    test.skip(!DEMO_EMAIL || !DEMO_PASSWORD, "set E2E_EMAIL and E2E_PASSWORD for an account in the demo organization");
    await signIn(page, DEMO_EMAIL!, DEMO_PASSWORD!);

    if (DEMO_DEPLOYMENT_ID) {
      await page.goto(`/app/deployments/${DEMO_DEPLOYMENT_ID}`);
    } else {
      await page.goto("/app/regressions?service=payment");
      const row = page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: "payment" }) }).first();
      await expect(row).toBeVisible();
      await row.press("Enter");
    }

    await expect(page).toHaveURL(/\/app\/deployments\/[0-9a-f-]+$/);
    await expect(page.getByRole("heading", { level: 1, name: /^payment / })).toBeVisible();
    const origin = page.getByRole("region", { name: "Probable origin" });
    await expect(origin).toBeVisible();
    await expect(origin.getByTestId("probable-origin")).toHaveText("payment");
    await expect(page.getByText("Regressed").first()).toBeVisible();
    await expect(page.getByRole("region", { name: "Before and after" })).toContainText("p95 latency");
    await expect(page.getByRole("region", { name: "Blast radius" }).getByRole("list", { name: "Legend" })).toContainText("Probable origin");
    await expect(page.getByText(/root cause/i)).toHaveCount(0);
  });
});
