import { z } from "zod";

/**
 * The API's error envelope (docs/dev/CONVENTIONS.md §4, rl_api.errors):
 * `{"error": {"code", "message", "request_id", "details"?}}`. FastAPI validation failures use
 * the same envelope with `code: "validation_error"` and `details: [{loc, msg, type}]`.
 */
export const errorEnvelopeSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().nullish(),
    details: z.unknown().optional(),
  }),
});

/** `402 plan_limit_reached` details (docs/billing/BILLING.md). */
export const planLimitDetailsSchema = z.object({
  limit: z.string(),
  plan: z.string(),
  max: z.number().nullish(),
  current: z.number().nullish(),
  upgrade: z.object({ plan: z.string(), url: z.string() }).nullish(),
});

export type PlanLimitDetails = z.infer<typeof planLimitDetailsSchema>;

export type ApiErrorKind = "http" | "network";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;
  readonly details: unknown;
  readonly kind: ApiErrorKind;
  /** Seconds from `Retry-After` on 429/503, when the API sent one. */
  readonly retryAfter: number | undefined;

  constructor(
    message: string,
    options: {
      status: number;
      code: string;
      requestId?: string;
      details?: unknown;
      kind?: ApiErrorKind;
      retryAfter?: number;
      cause?: unknown;
    },
  ) {
    super(message, { cause: options.cause });
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.requestId = options.requestId;
    this.details = options.details;
    this.kind = options.kind ?? "http";
    this.retryAfter = options.retryAfter;
  }

  get planLimit(): PlanLimitDetails | undefined {
    if (this.code !== "plan_limit_reached") return undefined;
    const parsed = planLimitDetailsSchema.safeParse(this.details);
    return parsed.success ? parsed.data : undefined;
  }

  /** Field-level messages from a 422 (`validation_error` or `weak_password`). */
  get fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {};
    if (this.code === "validation_error" && Array.isArray(this.details)) {
      for (const item of this.details) {
        const parsed = z
          .object({ loc: z.array(z.union([z.string(), z.number()])), msg: z.string() })
          .safeParse(item);
        if (!parsed.success) continue;
        const field = String(parsed.data.loc[parsed.data.loc.length - 1] ?? "");
        if (field && !out[field]) out[field] = parsed.data.msg;
      }
    }
    return out;
  }

  /** `weak_password` reasons (`details.reasons`). */
  get reasons(): string[] {
    const parsed = z.object({ reasons: z.array(z.string()) }).safeParse(this.details);
    return parsed.success ? parsed.data.reasons : [];
  }
}

const STATUS_FALLBACK: Record<number, string> = {
  400: "bad_request",
  401: "unauthorized",
  402: "payment_required",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  413: "payload_too_large",
  422: "validation_error",
  429: "rate_limited",
  500: "internal_error",
  502: "bad_gateway",
  503: "service_unavailable",
};

/** Builds an ApiError from a non-2xx response body (parsed JSON or undefined). */
export function parseApiError(
  status: number,
  body: unknown,
  headers?: Pick<Headers, "get">,
): ApiError {
  const envelope = errorEnvelopeSchema.safeParse(body);
  const requestIdHeader = headers?.get("x-request-id") ?? undefined;
  const retryRaw = headers?.get("retry-after");
  const retryAfter = retryRaw && /^\d+$/.test(retryRaw) ? Number(retryRaw) : undefined;
  if (envelope.success) {
    const { code, message, request_id, details } = envelope.data.error;
    return new ApiError(message, {
      status,
      code,
      requestId: request_id ?? requestIdHeader,
      details,
      retryAfter,
    });
  }
  const code = STATUS_FALLBACK[status] ?? `http_${status}`;
  return new ApiError(`The API answered HTTP ${status}`, {
    status,
    code,
    requestId: requestIdHeader,
    retryAfter,
  });
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/** One sentence for the UI. Keeps the API's own message where it is meant for people. */
export function describeApiError(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error && error.message ? error.message : "Something went wrong.";
  }
  if (error.kind === "network") return "The ReleaseLens API could not be reached. Check your connection and try again.";
  switch (error.code) {
    case "rate_limited":
      return error.retryAfter
        ? `Too many attempts. Try again in ${error.retryAfter} s.`
        : "Too many attempts. Wait a moment and try again.";
    case "forbidden":
      return "Your role in this organization does not allow this. Ask an owner or admin.";
    case "insufficient_scope":
      return "This needs a permission your session or key does not have.";
    case "plan_limit_reached":
      return "Your plan's limit is reached. Upgrade to add more.";
    case "internal_error":
      return error.requestId
        ? `The API hit an internal error (request ${error.requestId}).`
        : "The API hit an internal error.";
    default:
      return error.message || "Something went wrong.";
  }
}
