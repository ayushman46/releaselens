import createClient from "openapi-fetch";

import { ApiError, parseApiError, planLimitDetailsSchema, type PlanLimitDetails } from "@/lib/api/errors";
import type { paths } from "@/lib/api/schema";
import {
  CSRF_HEADER,
  getActiveOrgId,
  getCsrfToken,
  ORG_HEADER,
  setCsrfToken,
} from "@/lib/api/session-store";
import { API_BASE_URL } from "@/lib/env";
import { safeStorage } from "@/lib/storage";

/**
 * The browser's API client (docs/security/AUTH.md §3 "Front-end contract").
 *
 * Every request:
 * - is sent with `credentials: "include"` (the session lives in httpOnly cookies);
 * - carries `X-CSRF-Token` on POST/PUT/PATCH/DELETE (double-submit, from the `rl_csrf` cookie);
 * - carries `X-RL-Org` with the active organization (the tenant-implicit read routes need it);
 * - on `401`, triggers **one** refresh, then is retried **once**. Refreshes are serialised: in
 *   this tab by sharing one in-flight promise, across tabs by `navigator.locks` (two tabs
 *   presenting the same refresh token look like token theft and end the session);
 * - on a failed refresh, calls `onSessionExpired` (the app redirects to /login);
 * - on `402 plan_limit_reached`, calls `onPlanLimit` (the app shows the upgrade prompt).
 *
 * Errors come back as {@link ApiError}, parsed from the `{"error": {...}}` envelope.
 */

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const REFRESH_PATH = "/api/v1/auth/refresh";
const LOCK_NAME = "releaselens-auth-refresh";
const SHARED_REFRESH_KEY = "rl.auth.refreshedAt";
/**
 * After a successful refresh, a new 401 within this window is not an expired token (the
 * access token lives 15 minutes): the route refused the session itself. Refreshing again would
 * only rotate tokens for nothing, so the 401 is returned to the caller instead.
 */
export const REFRESH_COOLDOWN_MS = 30_000;

type RefreshOutcome = "refreshed" | "skipped" | "failed";

export interface LockManagerLike {
  request<T>(name: string, callback: () => Promise<T>): Promise<T>;
}

export interface ApiFetchOptions {
  baseUrl: string;
  /** The underlying fetch (tests inject one). */
  fetch?: typeof fetch;
  /** Cross-tab lock manager; defaults to `navigator.locks` when present. `null` disables it. */
  locks?: LockManagerLike | null;
  onSessionExpired?: () => void;
  onPlanLimit?: (details: PlanLimitDetails, error: ApiError) => void;
  now?: () => number;
}

function defaultLocks(): LockManagerLike | null {
  if (typeof navigator === "undefined") return null;
  const locks = (navigator as Navigator & { locks?: LockManagerLike }).locks;
  return locks && typeof locks.request === "function" ? locks : null;
}

function readSharedRefreshAt(): number {
  const raw = safeStorage.get(SHARED_REFRESH_KEY);
  const value = raw ? Number(raw) : 0;
  return Number.isFinite(value) ? value : 0;
}

export function createApiFetch(options: ApiFetchOptions) {
  const baseFetch = options.fetch ?? ((...args: Parameters<typeof fetch>) => globalThis.fetch(...args));
  const now = options.now ?? (() => Date.now());
  const locks = options.locks === undefined ? defaultLocks() : options.locks;
  const baseUrl = options.baseUrl.replace(/\/+$/, "");

  let inflight: Promise<RefreshOutcome> | null = null;
  let lastRefreshAt = 0;

  async function postRefresh(): Promise<RefreshOutcome> {
    let response: Response;
    try {
      response = await baseFetch(`${baseUrl}${REFRESH_PATH}`, {
        method: "POST",
        credentials: "include",
        headers: { accept: "application/json", [CSRF_HEADER]: getCsrfToken() ?? "" },
      });
    } catch {
      // Offline is not "signed out": keep the session and let the caller see a network error.
      return "skipped";
    }
    if (!response.ok) return "failed";
    const body: unknown = await response.json().catch(() => undefined);
    if (body && typeof body === "object" && "csrf_token" in body && typeof body.csrf_token === "string") {
      setCsrfToken(body.csrf_token);
    }
    lastRefreshAt = now();
    safeStorage.set(SHARED_REFRESH_KEY, String(lastRefreshAt));
    return "refreshed";
  }

  function refresh(requestStartedAt: number): Promise<RefreshOutcome> {
    if (inflight) return inflight;
    // A refresh finished after this request left: its retry will carry the new cookie.
    if (lastRefreshAt >= requestStartedAt) return Promise.resolve("refreshed");
    if (lastRefreshAt > 0 && now() - lastRefreshAt < REFRESH_COOLDOWN_MS) return Promise.resolve("skipped");

    const run = async (): Promise<RefreshOutcome> => {
      if (!locks) return postRefresh();
      return locks.request(LOCK_NAME, async () => {
        // Another tab refreshed while this one waited for the lock.
        const shared = readSharedRefreshAt();
        if (shared >= requestStartedAt) {
          lastRefreshAt = Math.max(lastRefreshAt, shared);
          return "refreshed";
        }
        return postRefresh();
      });
    };
    inflight = run()
      .catch((): RefreshOutcome => "failed")
      .finally(() => {
        inflight = null;
      });
    return inflight;
  }

  function prepare(request: Request): Request {
    const headers = new Headers(request.headers);
    if (!headers.has("accept")) headers.set("accept", "application/json");
    if (MUTATING.has(request.method.toUpperCase())) {
      const token = getCsrfToken();
      if (token) headers.set(CSRF_HEADER, token);
    }
    const org = getActiveOrgId();
    if (org && !headers.has(ORG_HEADER)) headers.set(ORG_HEADER, org);
    return new Request(request, { headers, credentials: "include" });
  }

  async function send(request: Request): Promise<Response> {
    try {
      return await baseFetch(request);
    } catch (cause) {
      if (request.signal?.aborted) throw cause;
      throw new ApiError("The ReleaseLens API could not be reached.", {
        status: 0,
        code: "network_error",
        kind: "network",
        cause,
      });
    }
  }

  async function notifyPlanLimit(response: Response): Promise<void> {
    if (response.status !== 402 || !options.onPlanLimit) return;
    const body: unknown = await response
      .clone()
      .json()
      .catch(() => undefined);
    const error = parseApiError(402, body, response.headers);
    const details = planLimitDetailsSchema.safeParse(error.details);
    if (error.code === "plan_limit_reached" && details.success) options.onPlanLimit(details.data, error);
  }

  return async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const original = input instanceof Request && init === undefined ? input : new Request(input, init);
    const isAuthRoute = new URL(original.url).pathname.startsWith("/api/v1/auth/");
    const startedAt = now();
    const first = prepare(original);
    // Keep an unread copy for the retry (a Request body can be read only once).
    const retryCopy = isAuthRoute ? undefined : first.clone();
    let response = await send(first);

    if (response.status === 401 && retryCopy) {
      const outcome = await refresh(startedAt);
      if (outcome === "refreshed") {
        response = await send(prepare(retryCopy));
        // Still 401 straight after a good refresh: the route refused the session itself
        // (e.g. it accepts API keys only). Report it; do not sign the user out.
      } else if (outcome === "failed") {
        options.onSessionExpired?.();
      }
    }
    await notifyPlanLimit(response);
    return response;
  };
}

export type ApiFetch = ReturnType<typeof createApiFetch>;

/** Hooks the app installs once (session provider): where to go when the session is gone, etc. */
const handlers: Pick<ApiFetchOptions, "onSessionExpired" | "onPlanLimit"> = {};

export function setApiHandlers(next: Pick<ApiFetchOptions, "onSessionExpired" | "onPlanLimit">): () => void {
  Object.assign(handlers, next);
  return () => {
    if (handlers.onSessionExpired === next.onSessionExpired) delete handlers.onSessionExpired;
    if (handlers.onPlanLimit === next.onPlanLimit) delete handlers.onPlanLimit;
  };
}

export const apiFetch = createApiFetch({
  baseUrl: API_BASE_URL,
  onSessionExpired: () => handlers.onSessionExpired?.(),
  onPlanLimit: (details, error) => handlers.onPlanLimit?.(details, error),
});

export function createApiClient(baseUrl: string, fetchImpl: ApiFetch) {
  return createClient<paths>({ baseUrl, fetch: fetchImpl, credentials: "include" });
}

/** Typed client for every path in openapi/releaselens.json (regenerate with `pnpm gen:api`). */
export const api = createApiClient(API_BASE_URL, apiFetch);

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Turns an openapi-fetch result into data, or throws the parsed {@link ApiError}. */
export async function unwrap<T>(promise: Promise<FetchResult<T>>): Promise<T> {
  const { data, error, response } = await promise;
  if (!response.ok) throw parseApiError(response.status, error, response.headers);
  return data as T;
}

/**
 * For routes not yet in the generated schema (the M5 risk read API). Same headers, refresh and
 * error handling as the typed client. `path` starts with `/api/v1/`.
 */
export async function requestJson<T>(
  path: string,
  init: RequestInit & { query?: Record<string, string | number | undefined | null> } = {},
  fetchImpl: ApiFetch = apiFetch,
  baseUrl: string = API_BASE_URL,
): Promise<T> {
  const url = new URL(`${baseUrl}${path}`);
  for (const [key, value] of Object.entries(init.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  const { query: _query, ...rest } = init;
  void _query;
  const response = await fetchImpl(url, { ...rest, credentials: "include" });
  const text = await response.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = undefined;
  }
  if (!response.ok) throw parseApiError(response.status, body, response.headers);
  return body as T;
}
