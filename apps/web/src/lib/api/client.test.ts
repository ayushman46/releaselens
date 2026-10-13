import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, createApiFetch, REFRESH_COOLDOWN_MS, requestJson, unwrap } from "@/lib/api/client";
import { ApiError, describeApiError, parseApiError } from "@/lib/api/errors";
import { resetSessionStore, setActiveOrgId, setCsrfToken } from "@/lib/api/session-store";

const BASE = "https://api.test";

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

const SESSION = {
  user: { id: "u1", email: "a@b.c", display_name: null, email_verified: true, has_password: true, github_linked: false },
  csrf_token: "csrf-from-refresh",
  access_expires_at: "2026-10-13T10:15:00Z",
  refresh_expires_at: "2026-10-27T10:00:00Z",
};

interface Call {
  url: string;
  method: string;
  headers: Headers;
  body: string;
}

/** A fake API: `/api/v1/auth/refresh` answers `refreshStatus`; other paths answer via `route`. */
function fakeApi(route: (call: Call, attempt: number) => Response | Promise<Response>, refreshStatus = 200) {
  const calls: Call[] = [];
  const attempts = new Map<string, number>();
  let releaseRefresh: (() => void) | undefined;
  const refreshGate = { hold: false };
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const call: Call = {
      url: request.url,
      method: request.method,
      headers: request.headers,
      body: request.body ? await request.text() : "",
    };
    calls.push(call);
    const path = new URL(call.url).pathname;
    if (path === "/api/v1/auth/refresh") {
      if (refreshGate.hold) await new Promise<void>((resolve) => (releaseRefresh = resolve));
      return refreshStatus === 200 ? json(200, SESSION) : json(refreshStatus, { error: { code: "unauthorized", message: "session expired; sign in again", request_id: "r-1" } });
    }
    const n = (attempts.get(path + call.method) ?? 0) + 1;
    attempts.set(path + call.method, n);
    return route(call, n);
  });
  return {
    fetchImpl,
    calls,
    refreshCalls: () => calls.filter((c) => new URL(c.url).pathname === "/api/v1/auth/refresh"),
    holdRefresh: () => (refreshGate.hold = true),
    releaseRefresh: () => releaseRefresh?.(),
  };
}

beforeEach(() => {
  resetSessionStore();
  document.cookie = "rl_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("API client: CSRF and org headers", () => {
  it("sends X-CSRF-Token from the rl_csrf cookie on mutations only, and X-RL-Org always", async () => {
    document.cookie = "rl_csrf=cookie-token; path=/";
    setActiveOrgId("org-1");
    const api = fakeApi(() => json(200, { ok: true }));
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null });

    await apiFetch(`${BASE}/api/v1/deployments`);
    await apiFetch(`${BASE}/api/v1/orgs/org-1/api-keys`, { method: "POST", body: "{}" });
    await apiFetch(`${BASE}/api/v1/orgs/org-1/api-keys/k`, { method: "DELETE" });

    const [get, post, del] = api.calls;
    expect(get!.headers.get("X-CSRF-Token")).toBeNull();
    expect(post!.headers.get("X-CSRF-Token")).toBe("cookie-token");
    expect(del!.headers.get("X-CSRF-Token")).toBe("cookie-token");
    for (const call of api.calls) expect(call.headers.get("X-RL-Org")).toBe("org-1");
  });

  it("falls back to the csrf_token the API returned when the cookie is not readable", async () => {
    setCsrfToken("from-session-body");
    const api = fakeApi(() => json(200, {}));
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null });
    await apiFetch(`${BASE}/api/v1/orgs`, { method: "POST", body: "{}" });
    expect(api.calls[0]!.headers.get("X-CSRF-Token")).toBe("from-session-body");
  });

  it("works through the typed openapi-fetch client (credentials, JSON body, CSRF)", async () => {
    document.cookie = "rl_csrf=typed; path=/";
    const api = fakeApi(() => json(201, { id: "o1", name: "Acme", slug: "acme", plan: "free", role: "owner", created_at: "x", limits: {} }));
    const client = createApiClient(BASE, createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null }));
    const org = await unwrap(client.POST("/api/v1/orgs", { body: { name: "Acme", slug: "acme" } }));
    expect(org.slug).toBe("acme");
    expect(api.calls[0]!.headers.get("X-CSRF-Token")).toBe("typed");
    expect(JSON.parse(api.calls[0]!.body)).toEqual({ name: "Acme", slug: "acme" });
  });
});

describe("API client: 401 → refresh → retry", () => {
  it("refreshes once and retries the original request (body included)", async () => {
    const api = fakeApi((_call, attempt) => (attempt === 1 ? json(401, { error: { code: "unauthorized", message: "expired" } }) : json(200, { ok: 1 })));
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null });
    const response = await apiFetch(`${BASE}/api/v1/orgs`, { method: "POST", body: JSON.stringify({ name: "x" }) });
    expect(response.status).toBe(200);
    expect(api.refreshCalls()).toHaveLength(1);
    const posts = api.calls.filter((c) => c.method === "POST" && c.url.endsWith("/api/v1/orgs"));
    expect(posts).toHaveLength(2);
    expect(posts[1]!.body).toBe(JSON.stringify({ name: "x" }));
    // The retry carries the CSRF token the refresh returned.
    expect(posts[1]!.headers.get("X-CSRF-Token")).toBe("csrf-from-refresh");
  });

  it("serialises concurrent refreshes: five parallel 401s share a single refresh", async () => {
    const api = fakeApi((_call, attempt) => (attempt === 1 ? json(401, {}) : json(200, {})));
    api.holdRefresh();
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null });
    const paths = ["/api/v1/a", "/api/v1/b", "/api/v1/c", "/api/v1/d", "/api/v1/e"];
    const pending = Promise.all(paths.map((p) => apiFetch(`${BASE}${p}`)));
    await vi.waitFor(() => expect(api.refreshCalls()).toHaveLength(1));
    api.releaseRefresh();
    const responses = await pending;
    expect(responses.map((r) => r.status)).toEqual([200, 200, 200, 200, 200]);
    expect(api.refreshCalls()).toHaveLength(1);
  });

  it("serialises across tabs through the lock manager and skips the refresh another tab already did", async () => {
    const api = fakeApi((_call, attempt) => (attempt === 1 ? json(401, {}) : json(200, {})));
    let clock = 1_000;
    const lockNames: string[] = [];
    const locks = {
      request: async <T,>(name: string, cb: () => Promise<T>) => {
        lockNames.push(name);
        // While this tab waited for the lock, another tab refreshed.
        localStorage.setItem("rl.auth.refreshedAt", String(clock + 5));
        return cb();
      },
    };
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks, now: () => clock++ });
    const response = await apiFetch(`${BASE}/api/v1/me`);
    expect(response.status).toBe(200);
    expect(lockNames).toEqual(["releaselens-auth-refresh"]);
    expect(api.refreshCalls()).toHaveLength(0);
  });

  it("calls onSessionExpired when the refresh fails, and does not retry", async () => {
    const onSessionExpired = vi.fn();
    const api = fakeApi(() => json(401, {}), 401);
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null, onSessionExpired });
    const response = await apiFetch(`${BASE}/api/v1/me`);
    expect(response.status).toBe(401);
    expect(onSessionExpired).toHaveBeenCalledTimes(1);
    expect(api.calls.filter((c) => c.url.endsWith("/api/v1/me"))).toHaveLength(1);
  });

  it("never refreshes for the auth routes themselves (no loop on a failed login)", async () => {
    const api = fakeApi(() => json(401, { error: { code: "invalid_credentials", message: "wrong" } }));
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null });
    await apiFetch(`${BASE}/api/v1/auth/login`, { method: "POST", body: "{}" });
    expect(api.refreshCalls()).toHaveLength(0);
  });

  it("does not sign out or refresh again when a route keeps refusing a fresh session", async () => {
    const onSessionExpired = vi.fn();
    let clock = 0;
    const api = fakeApi(() => json(401, { error: { code: "unauthorized", message: "missing or invalid API key" } }));
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null, onSessionExpired, now: () => (clock += 10) });
    expect((await apiFetch(`${BASE}/api/v1/services`)).status).toBe(401);
    expect((await apiFetch(`${BASE}/api/v1/services`)).status).toBe(401);
    expect(api.refreshCalls()).toHaveLength(1);
    expect(onSessionExpired).not.toHaveBeenCalled();
    clock += REFRESH_COOLDOWN_MS;
    await apiFetch(`${BASE}/api/v1/services`);
    expect(api.refreshCalls()).toHaveLength(2);
  });
});

describe("API client: errors", () => {
  it("parses the error envelope into an ApiError", async () => {
    const api = fakeApi(() =>
      json(409, { error: { code: "last_owner", message: "the last owner cannot be removed", request_id: "req-9" } }),
    );
    const client = createApiClient(BASE, createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null }));
    const error = await unwrap(client.DELETE("/api/v1/orgs/{org_id}/members/{user_id}", { params: { path: { org_id: "o", user_id: "u" } } })).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "last_owner", requestId: "req-9", message: "the last owner cannot be removed" });
  });

  it("falls back to a status code when the body is not an envelope", () => {
    const error = parseApiError(503, "<html>bad gateway</html>", new Headers({ "x-request-id": "h-1", "retry-after": "7" }));
    expect(error).toMatchObject({ status: 503, code: "service_unavailable", requestId: "h-1", retryAfter: 7 });
  });

  it("exposes validation field errors and weak-password reasons", () => {
    const validation = parseApiError(422, {
      error: { code: "validation_error", message: "request validation failed", details: [{ loc: ["body", "slug"], msg: "String should match pattern", type: "x" }] },
    });
    expect(validation.fieldErrors).toEqual({ slug: "String should match pattern" });
    const weak = parseApiError(422, { error: { code: "weak_password", message: "weak", details: { reasons: ["must be at least 12 characters"] } } });
    expect(weak.reasons).toEqual(["must be at least 12 characters"]);
  });

  it("reports plan_limit_reached through onPlanLimit with the upgrade details", async () => {
    const onPlanLimit = vi.fn();
    const body = {
      error: {
        code: "plan_limit_reached",
        message: "the free plan allows 1 project",
        details: { limit: "projects", plan: "free", max: 1, current: 1, upgrade: { plan: "team", url: "http://web/app/settings/billing" } },
      },
    };
    const api = fakeApi(() => json(402, body));
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: api.fetchImpl, locks: null, onPlanLimit });
    const error = await requestJson(`/api/v1/orgs/o/projects`, { method: "POST", body: "{}" }, apiFetch, BASE).catch((e: unknown) => e);
    expect(onPlanLimit).toHaveBeenCalledWith(expect.objectContaining({ limit: "projects", max: 1, upgrade: expect.objectContaining({ plan: "team" }) }), expect.any(ApiError));
    expect((error as ApiError).planLimit?.plan).toBe("free");
    expect(describeApiError(error)).toMatch(/Upgrade/);
  });

  it("turns a network failure into an ApiError of kind network", async () => {
    const apiFetch = createApiFetch({ baseUrl: BASE, fetch: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")), locks: null });
    const error = await apiFetch(`${BASE}/api/v1/me`).catch((e: unknown) => e);
    expect(error).toMatchObject({ kind: "network", code: "network_error" });
    expect(describeApiError(error)).toMatch(/could not be reached/);
  });
});
