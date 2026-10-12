import { z } from "zod";

/** `GET /healthz` — liveness, no dependencies checked. */
export const livenessSchema = z.object({
  status: z.literal("ok"),
});

/** One entry of `checks` in the `GET /readyz` body. `error` is present only on failure. */
export const dependencyCheckSchema = z.object({
  status: z.enum(["ok", "error"]),
  latency_ms: z.number().nonnegative(),
  error: z.string().optional(),
});

/** `GET /readyz` — 200 when every dependency is ok, 503 (same body shape) otherwise. */
export const readinessSchema = z.object({
  status: z.enum(["ready", "not_ready"]),
  checks: z.record(z.string(), dependencyCheckSchema),
});

/** Shared API error envelope (docs/dev/CONVENTIONS.md §4). */
export const apiErrorSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
  }),
});

export type Liveness = z.infer<typeof livenessSchema>;
export type DependencyCheck = z.infer<typeof dependencyCheckSchema>;
export type Readiness = z.infer<typeof readinessSchema>;

export type HealthApiErrorKind = "network" | "timeout" | "http" | "invalid_response";

export class HealthApiError extends Error {
  readonly kind: HealthApiErrorKind;
  readonly httpStatus: number | undefined;
  readonly requestId: string | undefined;

  constructor(
    message: string,
    kind: HealthApiErrorKind,
    options: { httpStatus?: number; requestId?: string; cause?: unknown } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "HealthApiError";
    this.kind = kind;
    this.httpStatus = options.httpStatus;
    this.requestId = options.requestId;
  }

  /** The API could not be reached at all (as opposed to answering with a bad response). */
  get isUnreachable(): boolean {
    return this.kind === "network" || this.kind === "timeout";
  }
}

function parseWith<S extends z.ZodType>(schema: S, body: unknown, endpoint: string): z.infer<S> {
  const result = schema.safeParse(body);
  if (!result.success) {
    throw new HealthApiError(
      `Unexpected response from ${endpoint}: ${z.prettifyError(result.error)}`,
      "invalid_response",
      { cause: result.error },
    );
  }
  return result.data;
}

export function parseLiveness(body: unknown): Liveness {
  return parseWith(livenessSchema, body, "/healthz");
}

export function parseReadiness(body: unknown): Readiness {
  return parseWith(readinessSchema, body, "/readyz");
}

/** Known dependencies first, in a stable order; anything else alphabetically after. */
const DEPENDENCY_ORDER = ["postgres", "kafka"];

const DEPENDENCY_LABELS: Record<string, string> = {
  postgres: "PostgreSQL",
  kafka: "Kafka",
};

export function dependencyLabel(name: string): string {
  return DEPENDENCY_LABELS[name] ?? name;
}

export function sortedChecks(checks: Readiness["checks"]): Array<[string, DependencyCheck]> {
  const rank = (name: string) => {
    const index = DEPENDENCY_ORDER.indexOf(name);
    return index === -1 ? DEPENDENCY_ORDER.length : index;
  };
  return Object.entries(checks).sort(
    ([a], [b]) => rank(a) - rank(b) || a.localeCompare(b),
  );
}

export function formatLatency(ms: number): string {
  if (ms < 1) return "<1 ms";
  if (ms < 10) return `${ms.toFixed(1)} ms`;
  return `${Math.round(ms).toLocaleString("en-US")} ms`;
}

export interface ProbeResult<T> {
  data: T;
  httpStatus: number;
  requestId: string | undefined;
}

export const PROBE_TIMEOUT_MS = 5_000;

interface ProbeOptions {
  /** Cancellation from the caller (TanStack Query passes one). */
  signal?: AbortSignal;
  timeoutMs?: number;
}

async function probe<T>(
  baseUrl: string,
  path: "/healthz" | "/readyz",
  parse: (body: unknown) => T,
  acceptedStatuses: readonly number[],
  { signal, timeoutMs = PROBE_TIMEOUT_MS }: ProbeOptions,
): Promise<ProbeResult<T>> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const forwardAbort = () => controller.abort(signal?.reason);
  if (signal?.aborted) forwardAbort();
  signal?.addEventListener("abort", forwardAbort, { once: true });

  try {
    let response: Response;
    let body: unknown;
    try {
      response = await fetch(`${baseUrl}${path}`, {
        cache: "no-store",
        headers: { accept: "application/json" },
        signal: controller.signal,
      });
      // A non-JSON body is left to the schema check below; an aborted read is not.
      body = await response.json().catch((error: unknown) => {
        if (controller.signal.aborted) throw error;
        return undefined;
      });
    } catch (error) {
      if (timedOut) {
        throw new HealthApiError(
          `No response from ${path} within ${timeoutMs / 1000} s`,
          "timeout",
          { cause: error },
        );
      }
      // Caller cancellation: rethrow untouched so the query layer treats it as cancelled.
      if (signal?.aborted) throw error;
      const reason = error instanceof Error && error.message ? `: ${error.message}` : "";
      throw new HealthApiError(`GET ${path} failed${reason}`, "network", { cause: error });
    }

    const requestId = response.headers.get("x-request-id") ?? undefined;

    if (!acceptedStatuses.includes(response.status)) {
      const envelope = apiErrorSchema.safeParse(body);
      const detail = envelope.success ? `: ${envelope.data.error.message}` : "";
      throw new HealthApiError(`${path} returned HTTP ${response.status}${detail}`, "http", {
        httpStatus: response.status,
        requestId: requestId ?? (envelope.success ? envelope.data.error.request_id : undefined),
      });
    }

    return { data: parse(body), httpStatus: response.status, requestId };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

export function fetchLiveness(
  baseUrl: string,
  options: ProbeOptions = {},
): Promise<ProbeResult<Liveness>> {
  return probe(baseUrl, "/healthz", parseLiveness, [200], options);
}

/** A 503 carries a valid not-ready body, so it is a result, not an error. */
export function fetchReadiness(
  baseUrl: string,
  options: ProbeOptions = {},
): Promise<ProbeResult<Readiness>> {
  return probe(baseUrl, "/readyz", parseReadiness, [200, 503], options);
}
