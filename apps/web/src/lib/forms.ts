import { z } from "zod";

/**
 * Client-side validation for every form. It catches obvious mistakes before a round trip; the
 * API stays authoritative (e.g. its password policy also rejects common and decorated words,
 * and answers `422 weak_password` with the reasons, which the forms show as well).
 */

const CONTROL = /[\u0000-\u001f\u007f]/;

export const emailSchema = z
  .string()
  .trim()
  .min(1, "Enter your e-mail address.")
  .max(320, "That address is too long.")
  .email("Enter a valid e-mail address, like name@company.com.");

/** Mirrors the cheap parts of rl_api.passwords: length, control characters, variety. */
export const newPasswordSchema = z
  .string()
  .min(12, "Use at least 12 characters.")
  .max(256, "Use at most 256 characters.")
  .refine((value) => !CONTROL.test(value), "Remove control characters (tabs, line breaks).")
  .refine((value) => new Set(value).size > 3, "Use more than 3 different characters.");

export const loginSchema = z.object({
  email: emailSchema,
  password: z.string().min(1, "Enter your password."),
});

export const signupSchema = z
  .object({
    display_name: z.string().trim().max(200, "Use at most 200 characters.").optional(),
    email: emailSchema,
    password: newPasswordSchema,
  })
  .refine((v) => !v.password.toLowerCase().includes(v.email.split("@")[0]!.toLowerCase()) || v.email.split("@")[0]!.length < 3, {
    path: ["password"],
    message: "Don't include your e-mail name in the password.",
  })
  .refine((v) => !v.password.toLowerCase().includes("releaselens"), {
    path: ["password"],
    message: "Don't include the product name in the password.",
  });

export const forgotPasswordSchema = z.object({ email: emailSchema });

export const resetPasswordSchema = z
  .object({
    new_password: newPasswordSchema,
    confirm: z.string(),
  })
  .refine((v) => v.new_password === v.confirm, { path: ["confirm"], message: "The passwords do not match." });

export const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}$/;

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

export const orgSchema = z.object({
  name: z.string().trim().min(1, "Name your organization.").max(200, "Use at most 200 characters."),
  slug: z
    .string()
    .trim()
    .regex(SLUG_PATTERN, "2–63 characters: lowercase letters, digits and hyphens, starting with a letter or digit."),
});

export const projectSchema = z.object({
  name: z.string().trim().min(1, "Name the project.").max(200, "Use at most 200 characters."),
  slug: z
    .string()
    .trim()
    .regex(SLUG_PATTERN, "2–63 characters: lowercase letters, digits and hyphens, starting with a letter or digit."),
});

export const ROLES = ["owner", "admin", "member", "viewer"] as const;

export const inviteSchema = z.object({
  email: emailSchema,
  role: z.enum(ROLES),
});

export const API_KEY_SCOPES = ["deployments:write", "telemetry:write", "risk:write", "read"] as const;

export const apiKeySchema = z.object({
  name: z.string().trim().min(1, "Name the key, e.g. “GitHub Actions – payments”.").max(120, "Use at most 120 characters."),
  scopes: z.array(z.enum(API_KEY_SCOPES)).min(1, "Choose at least one scope."),
  expires_at: z
    .string()
    .optional()
    .refine((v) => !v || new Date(v).getTime() > Date.now(), "Choose a date in the future."),
});

export const SLACK_WEBHOOK_PATTERN = /^https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9/_-]+$/;

export const SEVERITY_MIN_BANDS = ["HIGH", "CRITICAL"] as const;
export const RISK_MIN_BANDS = ["HIGH", "CRITICAL"] as const;

export const alertRuleSchema = z
  .object({
    name: z.string().trim().min(1, "Name the rule.").max(200, "Use at most 200 characters."),
    enabled: z.boolean(),
    on_regression: z.boolean(),
    regression_min_band: z.enum(SEVERITY_MIN_BANDS),
    on_risk: z.boolean(),
    risk_min_band: z.enum(RISK_MIN_BANDS),
    /** Write-only: empty keeps the stored webhook (on edit) or sets none (on create). */
    slack_webhook_url: z
      .string()
      .trim()
      .optional()
      .refine((v) => !v || SLACK_WEBHOOK_PATTERN.test(v), "Use a Slack incoming-webhook URL: https://hooks.slack.com/services/…"),
    email_recipients: z
      .string()
      .optional()
      .transform((v) =>
        (v ?? "")
          .split(/[\s,;]+/)
          .map((s) => s.trim())
          .filter(Boolean),
      )
      .pipe(z.array(emailSchema).max(20, "Up to 20 recipients.")),
  })
  .refine((v) => v.on_regression || v.on_risk, { path: ["on_regression"], message: "Turn on at least one trigger." });

export type FieldErrors = Partial<Record<string, string>>;

/** First message per field from a zod error (`path[0]` is the field name). */
export function zodFieldErrors(error: z.ZodError): FieldErrors {
  const out: FieldErrors = {};
  for (const issue of error.issues) {
    const key = String(issue.path[0] ?? "form");
    if (!out[key]) out[key] = issue.message;
  }
  return out;
}

/** Reads a `<form>` into a plain object of strings (checkbox groups become arrays). */
export function readForm(form: HTMLFormElement): Record<string, string | string[] | boolean> {
  const data = new FormData(form);
  const out: Record<string, string | string[] | boolean> = {};
  for (const key of new Set(data.keys())) {
    const values = data.getAll(key).map(String);
    const element = form.elements.namedItem(key);
    const isCheckboxGroup = element instanceof RadioNodeList && Array.from(element).some((e) => (e as HTMLInputElement).type === "checkbox");
    out[key] = isCheckboxGroup ? values : (values[0] ?? "");
  }
  return out;
}
