import { describe, expect, it } from "vitest";

import {
  alertRuleSchema,
  apiKeySchema,
  inviteSchema,
  loginSchema,
  orgSchema,
  resetPasswordSchema,
  signupSchema,
  slugify,
  zodFieldErrors,
} from "@/lib/forms";

function errors(result: { success: boolean; error?: unknown }) {
  return result.success ? {} : zodFieldErrors(result.error as Parameters<typeof zodFieldErrors>[0]);
}

describe("auth form validation", () => {
  it("login needs a valid e-mail and a password", () => {
    expect(errors(loginSchema.safeParse({ email: "", password: "" }))).toEqual({
      email: "Enter your e-mail address.",
      password: "Enter your password.",
    });
    expect(errors(loginSchema.safeParse({ email: "not-an-email", password: "x" })).email).toMatch(/valid e-mail/);
    expect(loginSchema.safeParse({ email: "  dev@acme.io ", password: "x" })).toMatchObject({ success: true, data: { email: "dev@acme.io" } });
  });

  it("signup mirrors the API's cheap password rules (length, variety, e-mail name, product name)", () => {
    expect(errors(signupSchema.safeParse({ email: "dev@acme.io", password: "short" })).password).toBe("Use at least 12 characters.");
    expect(errors(signupSchema.safeParse({ email: "dev@acme.io", password: "aaaaaaaaaaaaaaa" })).password).toBe("Use more than 3 different characters.");
    expect(errors(signupSchema.safeParse({ email: "dev@acme.io", password: "x".repeat(257) })).password).toBe("Use at most 256 characters.");
    expect(errors(signupSchema.safeParse({ email: "ayush@acme.io", password: "my-ayush-horse-battery" })).password).toMatch(/e-mail name/);
    expect(errors(signupSchema.safeParse({ email: "dev@acme.io", password: "ReleaseLens-rocks-42" })).password).toMatch(/product name/);
    expect(errors(signupSchema.safeParse({ email: "dev@acme.io", password: "tab\there-and-there" })).password).toMatch(/control characters/);
    expect(signupSchema.safeParse({ email: "dev@acme.io", password: "correct horse battery staple", display_name: "Dev" }).success).toBe(true);
  });

  it("reset requires matching passwords", () => {
    expect(errors(resetPasswordSchema.safeParse({ new_password: "correct horse battery", confirm: "correct horse batter" }))).toEqual({
      confirm: "The passwords do not match.",
    });
    expect(resetPasswordSchema.safeParse({ new_password: "correct horse battery", confirm: "correct horse battery" }).success).toBe(true);
  });
});

describe("organization and settings validation", () => {
  it("slugs follow the API pattern", () => {
    expect(slugify("  Acme Payments, Inc. ")).toBe("acme-payments-inc");
    expect(slugify("Ünïcödé Org")).toBe("unicode-org");
    expect(orgSchema.safeParse({ name: "Acme", slug: "acme" }).success).toBe(true);
    expect(errors(orgSchema.safeParse({ name: "", slug: "-bad" }))).toMatchObject({ name: "Name your organization.", slug: expect.stringMatching(/lowercase/) });
    expect(orgSchema.safeParse({ name: "A", slug: "a" }).success).toBe(false);
  });

  it("invites need an e-mail and a known role", () => {
    expect(inviteSchema.safeParse({ email: "x@y.io", role: "admin" }).success).toBe(true);
    expect(inviteSchema.safeParse({ email: "x@y.io", role: "superuser" }).success).toBe(false);
  });

  it("API keys need a name and at least one known scope", () => {
    expect(errors(apiKeySchema.safeParse({ name: "", scopes: [] }))).toMatchObject({ name: expect.any(String), scopes: "Choose at least one scope." });
    expect(apiKeySchema.safeParse({ name: "ci", scopes: ["deployments:write", "admin"] }).success).toBe(false);
    expect(errors(apiKeySchema.safeParse({ name: "ci", scopes: ["read"], expires_at: "2001-01-01" })).expires_at).toMatch(/future/);
  });

  it("alert rules accept only Slack incoming-webhook URLs and up to 20 e-mail recipients", () => {
    const base = { name: "Payments", enabled: true, on_regression: true, regression_min_band: "HIGH", on_risk: false, risk_min_band: "HIGH" };
    expect(alertRuleSchema.safeParse({ ...base, slack_webhook_url: "https://hooks.slack.com/services/T0/B0/xyz", email_recipients: "a@b.io, c@d.io" })).toMatchObject({
      success: true,
      data: { email_recipients: ["a@b.io", "c@d.io"] },
    });
    expect(errors(alertRuleSchema.safeParse({ ...base, slack_webhook_url: "https://evil.example/hook" })).slack_webhook_url).toMatch(/hooks\.slack\.com/);
    expect(alertRuleSchema.safeParse({ ...base, email_recipients: "not-an-email" }).success).toBe(false);
    const many = Array.from({ length: 21 }, (_, i) => `u${i}@x.io`).join(",");
    expect(alertRuleSchema.safeParse({ ...base, email_recipients: many }).success).toBe(false);
    expect(errors(alertRuleSchema.safeParse({ ...base, on_regression: false, on_risk: false })).on_regression).toBe("Turn on at least one trigger.");
  });
});
