import { HealthApiError, type ProbeResult, type Readiness } from "@/lib/api/health";

/** Every state a status indicator can show. */
export type HealthState = "ok" | "ready" | "not_ready" | "error" | "unreachable" | "checking";

/** The subset of a TanStack Query result the status logic needs. */
export interface QuerySnapshot<T> {
  status: "pending" | "error" | "success";
  data?: T;
  error?: unknown;
}

function errorState(error: unknown): HealthState {
  return error instanceof HealthApiError && error.isUnreachable ? "unreachable" : "error";
}

export function livenessState(query: QuerySnapshot<unknown>): HealthState {
  if (query.status === "error") return errorState(query.error);
  if (query.status === "pending") return "checking";
  return "ok";
}

export function readinessState(query: QuerySnapshot<ProbeResult<Readiness>>): HealthState {
  if (query.status === "error") return errorState(query.error);
  if (query.status === "pending" || !query.data) return "checking";
  return query.data.data.status;
}

/**
 * One headline state for the API. An error wins over stale data: TanStack Query keeps the
 * last successful `data` after a failed refetch, and showing "Ready" then would be wrong.
 */
export function overallState(
  liveness: QuerySnapshot<unknown>,
  readiness: QuerySnapshot<ProbeResult<Readiness>>,
): HealthState {
  const states = [livenessState(liveness), readinessState(readiness)];
  if (states.includes("unreachable")) return "unreachable";
  if (states.includes("error")) return "error";
  if (states.includes("checking")) return "checking";
  if (states.includes("not_ready")) return "not_ready";
  return "ready";
}
